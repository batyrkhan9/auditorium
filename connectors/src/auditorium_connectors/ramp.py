"""Ramp, read through its MCP server, behind the contract's `Sources` interface.

Three parts:

- `McpRampClient` calls Ramp MCP tools and returns the raw payloads.
- The `map_*` functions turn those payloads into contract records. They are
  written against the shapes in Ramp's Developer API OpenAPI spec
  (https://docs.ramp.com/openapi/developer-api.json). No MCP payload has been
  captured yet, so the tool names and response shapes are unverified until a
  live connection works (see connectors/decisions/2026-11-09-ramp-endpoint.md).
- `RampSources` serves Ramp records live and takes the ledger, bank statements
  and policy from a local dataset, because Ramp does not hold them.

`connect_ramp` returns a `RampSources` when everything loads, and otherwise a
`LocalFallback` whose `describe()` says why Ramp was not live.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from auditorium_contracts import (
    Approval,
    Document,
    DocumentType,
    LedgerEntry,
    LocalSources,
    Person,
    SourceRef,
    Sources,
    SourceSystem,
    SourceUse,
    SpendPolicy,
    SpendTransaction,
    TransactionKind,
)

from .fallback import LocalFallback

# Card transaction states that are not spend.
SKIPPED_STATES = {"DECLINED", "ERROR"}


class RampUnavailable(Exception):
    """Ramp could not be read completely. The caller falls back to local data."""


class RampMappingError(RampUnavailable):
    """A Ramp payload does not fit the contract."""


# --- talking to the MCP server -------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


# Tool names are guesses until `tools/list` is seen on a live connection. Ramp's
# docs confirm the CamelCase style and only name `GetAllReducedUsers`; the
# `transactions_to_retrieve` argument comes from the Ramp CLI docs. A missing
# tool makes the connection fail with the list of tools the server does offer.
DEFAULT_TOOLS: dict[str, ToolCall] = {
    "transactions": ToolCall(
        "GetTransactions", {"transactions_to_retrieve": "all_transactions_across_entire_business"}
    ),
    "bills": ToolCall("GetBills"),
    "receipts": ToolCall("GetReceipts"),
    "users": ToolCall("GetAllReducedUsers"),
    "departments": ToolCall("GetDepartments"),
    "approvals": ToolCall("GetApprovals"),
}


@dataclass(frozen=True)
class RampPayloads:
    """Everything read from Ramp, as Ramp returned it."""

    transactions: list[dict[str, Any]]
    bills: list[dict[str, Any]]
    receipts: list[dict[str, Any]]
    users: list[dict[str, Any]]
    departments: list[dict[str, Any]]
    # Keyed by the Ramp id of the transaction or bill under approval.
    approvals: dict[str, dict[str, Any]]


class RampClient(Protocol):
    def fetch(self) -> RampPayloads: ...


def records_from(payload: Any, tool: str) -> list[dict[str, Any]]:
    """The list of records in a tool response. Refuses a response with more pages."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise RampUnavailable(f"tool {tool} returned {type(payload).__name__}, not records")
    page = payload.get("page")
    if (
        (isinstance(page, dict) and page.get("next"))
        or payload.get("next_cursor")
        or payload.get("next_page_cursor")
        or payload.get("has_more")
    ):
        # Reading only the first page would silently drop records.
        raise RampUnavailable(f"tool {tool} returned more than one page; pagination not built")
    for key in ("data", "result", "results", "items"):
        if isinstance(payload.get(key), list):
            return payload[key]
    raise RampUnavailable(f"tool {tool} returned no record list (keys: {sorted(payload)})")


def tool_payload(result: Any, tool: str) -> Any:
    """The JSON a `CallToolResult` carries, structured or as text."""
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    if result.is_error:
        raise RampUnavailable(f"tool {tool} failed: {' '.join(texts)[:300]}")
    if result.structured_content is not None:
        return result.structured_content
    try:
        return json.loads("".join(texts))
    except ValueError as error:
        raise RampUnavailable(f"tool {tool} did not return JSON") from error


class McpRampClient:
    """Reads Ramp through its MCP server (streamable HTTP, bearer token)."""

    def __init__(
        self,
        url: str,
        token: str,
        tools: Mapping[str, ToolCall] = DEFAULT_TOOLS,
        approvals_batch: int = 100,
    ):
        self.url = url
        self._token = token
        self.tools = dict(tools)
        self.approvals_batch = approvals_batch

    def fetch(self) -> RampPayloads:
        import anyio

        refused: list[str] = []
        try:
            return anyio.run(self._fetch, refused)
        except Exception as error:
            # The SDK hides the HTTP status (a 401 reads "Server returned an error response").
            if refused:
                raise RampUnavailable(refused[0]) from error
            raise

    async def _fetch(self, refused: list[str]) -> RampPayloads:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        async def note_refusal(response: Any) -> None:
            if response.status_code >= 400:
                await response.aread()
                refused.append(f"HTTP {response.status_code}: {response.text[:200]}")

        headers = {"Authorization": f"Bearer {self._token}"}
        async with create_mcp_http_client(headers=headers) as http:
            http.event_hooks["response"].append(note_refusal)
            async with streamable_http_client(self.url, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    offered = await self._tool_names(session)
                    missing = sorted(
                        key for key, call in self.tools.items() if call.name not in offered
                    )
                    if missing:
                        raise RampUnavailable(
                            f"server lacks tools for {missing}; it offers {sorted(offered)}"
                        )

                    async def load(key: str, **extra: Any) -> list[dict[str, Any]]:
                        call = self.tools[key]
                        result = await session.call_tool(call.name, {**call.arguments, **extra})
                        return records_from(tool_payload(result, call.name), call.name)

                    transactions = await load("transactions")
                    bills = await load("bills")
                    approvals: dict[str, dict[str, Any]] = {}
                    for entity_type, items in (("TRANSACTION", transactions), ("BILL", bills)):
                        ids = [item["id"] for item in items]
                        for start in range(0, len(ids), self.approvals_batch):
                            batch = ids[start : start + self.approvals_batch]
                            for chain in await load(
                                "approvals", entity_type=entity_type, entity_ids=batch
                            ):
                                approvals[chain["entity_id"]] = chain
                    return RampPayloads(
                        transactions=transactions,
                        bills=bills,
                        receipts=await load("receipts"),
                        users=await load("users"),
                        departments=await load("departments"),
                        approvals=approvals,
                    )

    @staticmethod
    async def _tool_names(session: Any) -> set[str]:
        names: set[str] = set()
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else None
            listing = await session.list_tools(params=params)
            names.update(tool.name for tool in listing.tools)
            cursor = listing.next_cursor
            if not cursor:
                return names


# --- mapping payloads to contract records --------------------------------------


def transaction_id(ramp_id: str) -> str:
    return f"ramp:transaction:{ramp_id}"


def bill_id(ramp_id: str) -> str:
    return f"ramp:bill:{ramp_id}"


def person_id(ramp_id: str) -> str:
    return f"ramp:user:{ramp_id}"


def receipt_document_id(ramp_id: str) -> str:
    return f"ramp:receipt:{ramp_id}"


def attachment_document_id(ramp_id: str) -> str:
    return f"ramp:bill_attachment:{ramp_id}"


def _day(value: str | None) -> date | None:
    """The calendar date of an ISO 8601 date or datetime, as Ramp wrote it."""
    if not value:
        return None
    return datetime.fromisoformat(value).date() if "T" in value else date.fromisoformat(value)


def _usd_cents(money: Mapping[str, Any] | None, what: str) -> int:
    """Ramp's ApiSignedAmount {value, currency} or CurrencyAmount {amount, currency_code}."""
    if not money:
        raise RampMappingError(f"{what}: no amount")
    currency = money.get("currency") or money.get("currency_code")
    value = money["value"] if "value" in money else money.get("amount")
    if currency != "USD" or money.get("minor_unit_conversion_rate", 100) != 100:
        raise RampMappingError(f"{what}: amount is not in US cents ({currency})")
    if not isinstance(value, int):
        raise RampMappingError(f"{what}: amount {value!r} is not an integer")
    return value


def _gl_account(selections: Iterable[Mapping[str, Any]] | None) -> str | None:
    names = {
        selection.get("name")
        for selection in selections or ()
        if (selection.get("category_info") or {}).get("type") == "GL_ACCOUNT"
    }
    names.discard(None)
    return names.pop() if len(names) == 1 else None


def map_approvals(chain: Mapping[str, Any] | None) -> list[Approval]:
    """Decisions recorded in a Ramp approval chain. Steps still waiting are not decisions."""
    decisions = {"APPROVED": "approved", "REJECTED": "rejected"}
    approvals = []
    for step in (chain or {}).get("approval_history") or ():
        decision = decisions.get(str(step.get("step_status", "")).upper())
        if decision and step.get("user_id") and step.get("action_at"):
            approvals.append(
                Approval(
                    approver_id=person_id(step["user_id"]),
                    decision=decision,
                    decided_at=datetime.fromisoformat(step["action_at"]),
                )
            )
    return approvals


def map_card_transaction(
    payload: Mapping[str, Any], approvals: Mapping[str, Any] | None = None
) -> SpendTransaction:
    ramp_id = payload["id"]
    what = f"transaction {ramp_id}"
    vendor = payload.get("merchant_name") or payload.get("merchant_descriptor")
    purchased = _day(payload.get("user_transaction_time"))
    if not vendor or purchased is None:
        raise RampMappingError(f"{what}: no merchant name or no transaction time")
    holder = payload.get("card_holder") or {}
    return SpendTransaction(
        id=transaction_id(ramp_id),
        kind=TransactionKind.CARD_EXPENSE,
        source=SourceRef(system=SourceSystem.RAMP, record_type="transaction", record_id=ramp_id),
        vendor_name=vendor,
        amount_cents=_usd_cents(payload.get("entity_amount"), what),
        transaction_date=purchased,
        accounting_date=_day(payload.get("accounting_date")),
        memo=payload.get("memo") or None,
        category=_gl_account(payload.get("accounting_field_selections")),
        spender_id=person_id(holder["user_id"]) if holder.get("user_id") else None,
        department=holder.get("department_name") or None,
        document_ids=[receipt_document_id(r) for r in payload.get("receipts") or ()],
        approvals=map_approvals(approvals),
        raw=dict(payload),
    )


def map_bill(
    payload: Mapping[str, Any], approvals: Mapping[str, Any] | None = None
) -> SpendTransaction:
    ramp_id = payload["id"]
    what = f"bill {ramp_id}"
    vendor = (payload.get("vendor") or {}).get("name")
    issued = _day(payload.get("issued_at"))
    if not vendor or issued is None:
        raise RampMappingError(f"{what}: no vendor name or no invoice date")
    category = _gl_account(payload.get("accounting_field_selections")) or _gl_account(
        s
        for line in payload.get("line_items") or ()
        for s in line.get("accounting_field_selections") or ()
    )
    owner = payload.get("bill_owner") or {}
    return SpendTransaction(
        id=bill_id(ramp_id),
        kind=TransactionKind.VENDOR_BILL,
        source=SourceRef(system=SourceSystem.RAMP, record_type="bill", record_id=ramp_id),
        vendor_name=vendor,
        amount_cents=_usd_cents(payload.get("amount"), what),
        transaction_date=issued,
        accounting_date=_day(payload.get("accounting_date")),
        paid_date=_day(payload.get("paid_at")),
        due_date=_day(payload.get("due_at")),
        invoice_number=payload.get("invoice_number") or None,
        memo=payload.get("memo") or None,
        category=category,
        spender_id=person_id(owner["id"]) if owner.get("id") else None,
        document_ids=[attachment_document_id(a["id"]) for a in payload.get("attachments") or ()],
        approvals=map_approvals(approvals),
        raw=dict(payload),
    )


def map_user(payload: Mapping[str, Any], departments: Mapping[str, str]) -> Person:
    name = " ".join(p for p in (payload.get("first_name"), payload.get("last_name")) if p)
    manager = payload.get("manager_id")
    return Person(
        id=person_id(payload["id"]),
        name=name or payload.get("email") or payload["id"],
        email=payload.get("email") or None,
        # Ramp's `role` is a permission level (BUSINESS_ADMIN, ...), not a job role,
        # so it is not what approval rules match on. Ramp holds no job title.
        role=None,
        department=departments.get(payload.get("department_id") or ""),
        manager_id=person_id(manager) if manager else None,
    )


@dataclass(frozen=True)
class RemoteFile:
    """A document Ramp holds, before it is downloaded."""

    document_id: str
    type: DocumentType
    origin: str
    source: SourceRef
    url: str


ATTACHMENT_TYPES = {
    "INVOICE": (DocumentType.INVOICE, "external"),
    "VENDOR_CREDIT": (DocumentType.OTHER, "external"),
    "EMAIL": (DocumentType.OTHER, "internal"),
    "FILE": (DocumentType.OTHER, "internal"),
}


def remote_files(payloads: RampPayloads) -> list[RemoteFile]:
    files = []
    for receipt in payloads.receipts:
        files.append(
            RemoteFile(
                document_id=receipt_document_id(receipt["id"]),
                type=DocumentType.RECEIPT,
                origin="external",
                source=SourceRef(
                    system=SourceSystem.RAMP, record_type="receipt", record_id=receipt["id"]
                ),
                url=receipt["receipt_url"],
            )
        )
    for bill in payloads.bills:
        for attachment in bill.get("attachments") or ():
            doc_type, origin = ATTACHMENT_TYPES.get(
                attachment.get("attachment_type"), (DocumentType.OTHER, "internal")
            )
            files.append(
                RemoteFile(
                    document_id=attachment_document_id(attachment["id"]),
                    type=doc_type,
                    origin=origin,
                    source=SourceRef(
                        system=SourceSystem.RAMP,
                        record_type="bill_attachment",
                        record_id=attachment["id"],
                    ),
                    url=attachment["url"],
                )
            )
    return files


# Saves the file at `url` to `dest` and returns its media type.
Downloader = Callable[[str, Path], str]


def http_download(url: str, dest: Path) -> str:
    import httpx

    response = httpx.get(url, follow_redirects=True, timeout=60)
    response.raise_for_status()
    dest.write_bytes(response.content)
    return response.headers.get("content-type", "application/octet-stream").split(";")[0].strip()


# --- the Sources implementation -------------------------------------------------


class RampSources:
    """Card expenses, bills, their files and people live from Ramp; the rest from `local`.

    Everything is read when it is built, so a failure surfaces here and the
    caller can fall back before the agent starts.
    """

    def __init__(
        self,
        payloads: RampPayloads,
        local: Sources,
        *,
        endpoint: str,
        cache_dir: Path | str,
        download: Downloader = http_download,
    ):
        self.endpoint = endpoint
        self.local = local
        self.cache_dir = Path(cache_dir)
        approvals = payloads.approvals
        self._transactions = [
            map_card_transaction(t, approvals.get(t["id"]))
            for t in payloads.transactions
            if t.get("state") not in SKIPPED_STATES
        ] + [map_bill(b, approvals.get(b["id"])) for b in payloads.bills]
        departments = {d["id"]: d["name"] for d in payloads.departments if d.get("id")}
        self._people = [map_user(u, departments) for u in payloads.users]
        self._documents = {d.id: d for d in self._download(remote_files(payloads), download)}

        for txn in self._transactions:
            for document_id in txn.document_ids:
                if document_id not in self._documents:
                    raise RampUnavailable(
                        f"{txn.id} names {document_id}, which Ramp did not return"
                    )

    def _download(self, files: list[RemoteFile], download: Downloader) -> list[Document]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        documents = []
        for file in files:
            part = self.cache_dir / f"{file.source.record_type}-{file.source.record_id}.part"
            media_type = download(file.url, part)
            extension = mimetypes.guess_extension(media_type) or ".bin"
            name = f"{file.source.record_type}-{file.source.record_id}{extension}"
            part.replace(self.cache_dir / name)
            documents.append(
                Document(
                    id=file.document_id,
                    type=file.type,
                    source=file.source,
                    origin=file.origin,
                    path=name,
                    media_type=media_type,
                    sha256=hashlib.sha256((self.cache_dir / name).read_bytes()).hexdigest(),
                )
            )
        return documents

    def describe(self) -> list[SourceUse]:
        live = SourceUse(system=SourceSystem.RAMP, mode="live", detail=f"Ramp MCP {self.endpoint}")
        return [live] + [
            use for use in self.local.describe() if use.system is not SourceSystem.RAMP
        ]

    def transactions(self) -> list[SpendTransaction]:
        return list(self._transactions)

    def ledger_entries(self) -> list[LedgerEntry]:
        return self.local.ledger_entries()

    def documents(self) -> list[Document]:
        others = [d for d in self.local.documents() if d.source.system is not SourceSystem.RAMP]
        return list(self._documents.values()) + others

    def document_file(self, document_id: str) -> Path:
        if document_id in self._documents:
            return self.cache_dir / self._documents[document_id].path
        return self.local.document_file(document_id)

    def people(self) -> list[Person]:
        return list(self._people)

    def policy(self) -> SpendPolicy:
        return self.local.policy()


def _reason(error: BaseException) -> str:
    """One line from an error, looking inside the exception groups anyio raises."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}"[:500]


def connect_ramp(
    local: LocalSources,
    *,
    url: str | None,
    token: str | None,
    cache_dir: Path | str,
    client: RampClient | None = None,
    download: Downloader = http_download,
) -> Sources:
    """Live Ramp if it loads completely, otherwise the local dataset with the reason why."""
    if not url:
        return LocalFallback(local, {SourceSystem.RAMP: "RAMP_MCP_URL is not set"})
    if client is None and not token:
        return LocalFallback(
            local, {SourceSystem.RAMP: f"RAMP_MCP_TOKEN is not set; {url} needs an OAuth token"}
        )
    try:
        payloads = (client or McpRampClient(url, token or "")).fetch()
        return RampSources(payloads, local, endpoint=url, cache_dir=cache_dir, download=download)
    except Exception as error:
        return LocalFallback(local, {SourceSystem.RAMP: f"{url} failed: {_reason(error)}"})
