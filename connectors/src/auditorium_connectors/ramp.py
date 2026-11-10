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

A server that names itself `STANDIN_SERVER_NAME` in its MCP `initialize` answer is
our own stand-in (`ramp_standin.py`), not Ramp. Its records go through the same
client and mapping, but `describe()` reports Ramp as `local`.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
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

# Bills that are not spend. Ramp's spec gives Bill.status only OPEN or PAID; a deleted
# bill shows status_summary ARCHIVED, and a provisional DraftBill has status DRAFT.
# The spec has no voided status.
SKIPPED_BILL_STATUSES = {"DRAFT"}
SKIPPED_BILL_SUMMARIES = {"ARCHIVED"}

# Only these HTTP statuses say the server refused us. Others, like the 405 a
# streamable HTTP server may answer to the optional GET stream, are routine.
REFUSAL_STATUSES = {401, 403}

# A Ramp id becomes part of a cache file name, so it may hold nothing else.
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")

# The `serverInfo.name` our local stand-in for Ramp's MCP server gives in `initialize`.
STANDIN_SERVER_NAME = "auditorium-ramp-standin"


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
    # The `serverInfo` the MCP server sent in `initialize` (name, version, ...).
    server: dict[str, Any] | None = None


class RampClient(Protocol):
    def fetch(self) -> RampPayloads: ...


# Keys that say a response holds only part of the records.
CONTINUATION_KEYS = (
    "next",
    "next_cursor",
    "next_page_cursor",
    "cursor",
    "has_more",
    "truncated",
    "is_truncated",
)
COUNT_KEYS = ("total", "total_count", "totalCount", "count")


def records_from(payload: Any, tool: str) -> list[dict[str, Any]]:
    """The list of records in a tool response. Refuses a response that is not all of them."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        raise RampUnavailable(f"tool {tool} returned {type(payload).__name__}, not records")
    records = next(
        (
            payload[key]
            for key in ("data", "result", "results", "items")
            if isinstance(payload.get(key), list)
        ),
        None,
    )
    if records is None:
        raise RampUnavailable(f"tool {tool} returned no record list (keys: {sorted(payload)})")
    # Reading only the first page would silently drop records.
    page = payload.get("page")
    for where in (payload, page if isinstance(page, dict) else {}):
        marker = next((key for key in CONTINUATION_KEYS if where.get(key)), None)
        if marker:
            raise RampUnavailable(
                f"tool {tool} returned more than one page ({marker}); pagination not built"
            )
        for key in COUNT_KEYS:
            total = where.get(key)
            if isinstance(total, int) and not isinstance(total, bool) and total > len(records):
                raise RampUnavailable(
                    f"tool {tool} returned {len(records)} of {total} records; pagination not built"
                )
    return records


def approval_chains(
    entity_type: str, requested: list[str], chains: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Chains keyed by entity id. Refuses an answer that leaves out a requested id,
    which would otherwise read as "no approvals"."""
    found = {chain["entity_id"]: chain for chain in chains}
    missing = [entity_id for entity_id in requested if entity_id not in found]
    if missing:
        raise RampUnavailable(
            f"no approval chain returned for {len(missing)} {entity_type} id(s), first {missing[0]}"
        )
    return found


def refusal_recorder(refused: list[str]) -> Callable[[Any], Any]:
    """An httpx response hook that notes an authentication refusal, which the SDK hides."""

    async def note_refusal(response: Any) -> None:
        if response.status_code in REFUSAL_STATUSES:
            await response.aread()
            refused.append(f"HTTP {response.status_code}: {response.text[:200]}")

    return note_refusal


def _innermost(error: BaseException) -> BaseException:
    """The first leaf of the exception groups anyio raises."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


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
            cause = _innermost(error)
            if isinstance(cause, RampUnavailable):
                # Our own reason is the right one; a refusal noted earlier is not.
                raise cause from None
            # The SDK hides the HTTP status (a 401 reads "Server returned an error response").
            if refused:
                raise RampUnavailable(refused[0]) from error
            raise

    async def _fetch(self, refused: list[str]) -> RampPayloads:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        headers = {"Authorization": f"Bearer {self._token}"}
        async with create_mcp_http_client(headers=headers) as http:
            http.event_hooks["response"].append(refusal_recorder(refused))
            async with streamable_http_client(self.url, http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    initialized = await session.initialize()
                    server = initialized.server_info.model_dump(mode="json", exclude_none=True)
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
                            chains = await load(
                                "approvals", entity_type=entity_type, entity_ids=batch
                            )
                            approvals.update(approval_chains(entity_type, batch, chains))
                    return RampPayloads(
                        transactions=transactions,
                        bills=bills,
                        receipts=await load("receipts"),
                        users=await load("users"),
                        departments=await load("departments"),
                        approvals=approvals,
                        server=server,
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


# The query string and fragment of a URL, where presigned URLs keep their signature.
_URL_QUERY = re.compile(r"(https?://[^\s?#'\"]+)[?#][^\s'\"]*")


def _unsigned(value: Any) -> Any:
    """`value` with the query string and fragment cut from every URL in any string,
    at any depth. Ramp's file links (`attachments[].url`, `receipt_url`, `invoice_urls`)
    are presigned and the signature is a credential, so `raw` keeps scheme, host and
    path (enough to identify the file) and never the signature. Fields are kept, not
    dropped, so `raw` still has the original's shape."""
    if isinstance(value, str):
        return _URL_QUERY.sub(r"\1", value)
    if isinstance(value, Mapping):
        return {key: _unsigned(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_unsigned(item) for item in value]
    return value


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
        paid_date=_day(payload.get("settlement_date")),
        memo=payload.get("memo") or None,
        category=_gl_account(payload.get("accounting_field_selections")),
        spender_id=person_id(holder["user_id"]) if holder.get("user_id") else None,
        department=holder.get("department_name") or None,
        document_ids=[receipt_document_id(r) for r in payload.get("receipts") or ()],
        approvals=map_approvals(approvals),
        raw=_unsigned(payload),
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
        raw=_unsigned(payload),
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
    response = httpx.get(url, follow_redirects=True, timeout=60)
    response.raise_for_status()
    dest.write_bytes(response.content)
    return response.headers.get("content-type", "application/octet-stream").split(";")[0].strip()


def _download_failure(error: Exception) -> str:
    """Why a download failed, without the URL: Ramp's are presigned and carry a signature."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    return type(error).__name__


def _is_document(media_type: str, path: Path) -> bool:
    """A PDF or an image. A 200 login page is neither."""
    if media_type == "application/pdf":
        with path.open("rb") as file:
            return file.read(5) == b"%PDF-"
    return media_type.startswith("image/")


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
        self.server = dict(payloads.server or {})
        approvals = payloads.approvals
        self._transactions = [
            map_card_transaction(t, approvals.get(t["id"]))
            for t in payloads.transactions
            if t.get("state") not in SKIPPED_STATES
        ] + [
            map_bill(b, approvals.get(b["id"]))
            for b in payloads.bills
            if b.get("status") not in SKIPPED_BILL_STATUSES
            and b.get("status_summary") not in SKIPPED_BILL_SUMMARIES
        ]
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
            what = f"{file.source.record_type} {file.source.record_id}"
            if not SAFE_ID.fullmatch(file.source.record_id):
                raise RampUnavailable(
                    f"{file.source.record_type} id {file.source.record_id!r} is not a plain id"
                )
            part = self.cache_dir / f"{file.source.record_type}-{file.source.record_id}.part"
            try:
                media_type = download(file.url, part)
            except Exception as error:
                raise RampUnavailable(
                    f"could not download {what}: {_download_failure(error)}"
                ) from None
            if not _is_document(media_type, part):
                part.unlink(missing_ok=True)
                raise RampUnavailable(f"{what} is not a PDF or an image (served as {media_type})")
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

    @property
    def is_standin(self) -> bool:
        """True when the server said it is our stand-in. Decided from what the server
        sent, not from a setting, so a stand-in can't be configured into "live"."""
        return self.server.get("name") == STANDIN_SERVER_NAME

    def describe(self) -> list[SourceUse]:
        if self.is_standin:
            served = self.server.get("description") or "dataset not stated"
            ramp = SourceUse(
                system=SourceSystem.RAMP,
                mode="local",
                detail=f"Ramp stand-in MCP server at {self.endpoint}, {served}; not Ramp",
            )
        else:
            ramp = SourceUse(
                system=SourceSystem.RAMP, mode="live", detail=f"Ramp MCP {self.endpoint}"
            )
        return [ramp] + [
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
    """One line from an error, looking inside the exception groups anyio raises.
    URLs lose their query strings, so a presigned signature never reaches a report."""
    error = _innermost(error)
    return _URL_QUERY.sub(r"\1", f"{type(error).__name__}: {error}")[:500]


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
