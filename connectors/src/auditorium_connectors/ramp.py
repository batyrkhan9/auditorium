"""Ramp, read through its MCP server, behind the contract's `Sources` interface.

Three parts:

- `McpRampClient` calls Ramp MCP tools and returns the raw payloads.
- The `map_*` functions turn those payloads into contract records. Card
  transactions, users and org charts follow payloads captured from Ramp's sandbox
  MCP server on 2026-11-10 (connectors/tests/fixtures/ramp_sandbox_capture.json).
  The sandbox business has no bills and no receipts, so the bill, bill-invoice and
  receipt shapes still follow Ramp's Developer API spec and are unconfirmed (see
  connectors/decisions/2026-11-10-ramp-real-tools.md).
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
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

import httpx
from auditorium_contracts import (
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

# Card transaction states that are not spend. ramp_get_transactions leaves declines
# out unless asked; this is a second guard.
SKIPPED_STATES = {"DECLINED", "ERROR"}
# Not settled yet: the amount can still change and nothing is on a statement.
PENDING_STATES = {"PENDING"}

# Bills that are not spend. ramp_list_bills leaves archived bills out unless asked
# and lists submitted bills only; these are second guards on the Developer API's
# field names (no bill has been seen on the MCP server).
SKIPPED_BILL_STATUSES = {"DRAFT"}
SKIPPED_BILL_SUMMARIES = {"ARCHIVED"}

# Only these HTTP statuses say the server refused us. Others, like the 405 a
# streamable HTTP server may answer to the optional GET stream, are routine.
REFUSAL_STATUSES = {401, 403}

# A Ramp id becomes part of a cache file name, so it may hold nothing else.
SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")

# The `serverInfo.name` our local stand-in for Ramp's MCP server gives in `initialize`.
STANDIN_SERVER_NAME = "auditorium-ramp-standin"

# Contract v0.1 has no way to say "the source could not tell", so `describe()` says it.
APPROVALS_UNAVAILABLE = (
    "approvals not available from Ramp's MCP server (no approval instance is reachable "
    "from a transaction), so an empty approvals list means unknown, not unapproved"
)


class RampUnavailable(Exception):
    """Ramp could not be read completely. The caller falls back to local data."""


class RampMappingError(RampUnavailable):
    """A Ramp payload does not fit the contract."""


# --- talking to the MCP server -------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    # The response key that holds the records. None: the response is one object.
    records_key: str | None = None
    # The response field naming the next page, and the argument that takes it back.
    cursor_field: str | None = None
    cursor_argument: str | None = None


def _rationale(what: str) -> str:
    """Most Ramp tools require a `rationale`: why the call is made."""
    return (
        f"Auditorium audit preparation, read-only: load {what} to check receipts, "
        "approvals, ledger match and period before the auditor asks."
    )


# Names, arguments and response keys from Ramp's sandbox `tools/list` and responses
# (2026-11-10), except where marked unconfirmed. A missing tool makes the connection
# fail with the list of tools the server does offer.
DEFAULT_TOOLS: dict[str, ToolCall] = {
    "transactions": ToolCall(
        "ramp_get_transactions",
        {
            "rationale": _rationale("every card transaction"),
            "transactions_to_retrieve": "all_transactions_across_entire_business",
            "state": "all",  # every non-declined state
            "page_size": 200,
            "include_count": True,
            # Receipts and accounting categories are filled only when asked for.
            "details_to_include_in_response": ["submitted_items"],
        },
        records_key="transactions",
        cursor_field="next_page_cursor",
        cursor_argument="next_page_cursor",
    ),
    "bills": ToolCall(
        "ramp_list_bills",
        {
            "rationale": _rationale("every submitted vendor bill"),
            "include_paid": True,
            "include_archived": False,
            "limit": 50,
        },
        records_key="bills",
        cursor_field="next_page_cursor",
        cursor_argument="page_cursor",
    ),
    # Called once per bill with `bill_id`. Response key unconfirmed (no bills in the sandbox).
    "bill_invoices": ToolCall(
        "ramp_get_bill_invoices",
        {"rationale": _rationale("the invoice files behind a bill")},
        records_key="attachments",
    ),
    # Called with `receipt_uuids`. Response key unconfirmed (no receipts in the sandbox).
    "receipts": ToolCall(
        "ramp_get_receipt_details",
        {"rationale": _rationale("the receipts attached to card transactions")},
        records_key="receipts",
    ),
    "users": ToolCall(
        "ramp_list_users",
        {"rationale": _rationale("every person who spends or approves"), "page_size": 100},
        records_key="users",
        # Ramp answers `next_page` (a user id) and takes it back as `page_cursor`.
        cursor_field="next_page",
        cursor_argument="page_cursor",
    ),
    # Called once per user with `user_id`; managers come from `direct_reports`.
    "org_chart": ToolCall("ramp_get_org_chart", {"rationale": _rationale("who manages whom")}),
}


@dataclass(frozen=True)
class RampPayloads:
    """Everything read from Ramp, as Ramp returned it."""

    transactions: list[dict[str, Any]]
    bills: list[dict[str, Any]]
    # Keyed by the Ramp id of the bill.
    bill_invoices: dict[str, list[dict[str, Any]]]
    receipts: list[dict[str, Any]]
    users: list[dict[str, Any]]
    # Keyed by the Ramp id of the user asked about.
    org_charts: dict[str, dict[str, Any]]
    # The `serverInfo` the MCP server sent in `initialize` (name, version, ...).
    server: dict[str, Any] | None = None


class RampClient(Protocol):
    def fetch(self) -> RampPayloads: ...


# Keys that say a response holds only part of the records. The one a tool pages
# with (`cursor_field`) is followed; any other refuses the response.
CONTINUATION_KEYS = (
    "next",
    "next_page",
    "next_cursor",
    "next_page_cursor",
    "cursor",
    "has_more",
    "truncated",
    "is_truncated",
)
COUNT_KEYS = ("total_count", "total_found", "total", "totalCount", "count")
MAX_PAGES = 1000


def records_from(payload: Any, call: ToolCall) -> list[dict[str, Any]]:
    """The records on one page of a tool response. Refuses a page that says there is
    more in a way this tool does not page through."""
    tool = call.name
    if not isinstance(payload, dict):
        raise RampUnavailable(f"tool {tool} returned {type(payload).__name__}, not records")
    records = payload.get(call.records_key or "")
    if not isinstance(records, list):
        raise RampUnavailable(
            f"tool {tool} returned no {call.records_key!r} list (keys: {sorted(payload)})"
        )
    page = payload.get("page")
    for where in (payload, page if isinstance(page, dict) else {}):
        marker = next(
            (key for key in CONTINUATION_KEYS if key != call.cursor_field and where.get(key)),
            None,
        )
        if marker:
            raise RampUnavailable(
                f"tool {tool} returned more than one page ({marker}); pagination not built"
            )
    return records


def _counted(payload: Mapping[str, Any]) -> int:
    """The record total a page states, or 0 when it states none."""
    totals = [payload.get(key) for key in COUNT_KEYS]
    return max((t for t in totals if isinstance(t, int) and not isinstance(t, bool)), default=0)


# Calls one tool with the given arguments and returns its JSON.
CallTool = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def read_all(call_tool: CallTool, call: ToolCall, **extra: Any) -> list[dict[str, Any]]:
    """Every record a tool holds, following its page cursor. Never returns part of them."""
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = None
    total = 0  # the largest total any page stated
    for _ in range(MAX_PAGES):
        arguments = {**call.arguments, **extra}
        if cursor:
            arguments[call.cursor_argument or ""] = cursor
        payload = await call_tool(call.name, arguments)
        page = records_from(payload, call)
        records.extend(page)
        total = max(total, _counted(payload))
        cursor = payload.get(call.cursor_field) if call.cursor_field else None
        if not cursor or not page:
            if total > len(records):
                raise RampUnavailable(
                    f"tool {call.name} returned {len(records)} of {total} records"
                )
            return records
        if cursor in seen:
            raise RampUnavailable(f"tool {call.name} repeated a page cursor")
        seen.add(cursor)
    raise RampUnavailable(f"tool {call.name} gave more than {MAX_PAGES} pages")


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
    """The JSON a `CallToolResult` carries, structured or as text. Ramp reports some
    failures as a normal answer `{"error": true, "message": ...}`; those are refused."""
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    if result.is_error:
        raise RampUnavailable(f"tool {tool} failed: {' '.join(texts)[:300]}")
    if result.structured_content is not None:
        payload = result.structured_content
    else:
        try:
            payload = json.loads("".join(texts))
        except ValueError as error:
            raise RampUnavailable(f"tool {tool} did not return JSON") from error
    if isinstance(payload, dict) and payload.get("error") is True:
        raise RampUnavailable(f"tool {tool} failed: {str(payload.get('message'))[:300]}")
    return payload


class McpRampClient:
    """Reads Ramp through its MCP server (streamable HTTP, bearer token)."""

    def __init__(
        self,
        url: str,
        token: str,
        tools: Mapping[str, ToolCall] = DEFAULT_TOOLS,
        receipts_batch: int = 50,
    ):
        self.url = url
        self._token = token
        self.tools = dict(tools)
        self.receipts_batch = receipts_batch

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

                    async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
                        return tool_payload(await session.call_tool(name, arguments), name)

                    tools = self.tools
                    transactions = await read_all(call_tool, tools["transactions"])
                    bills = await read_all(call_tool, tools["bills"])
                    bill_invoices = {
                        bill["id"]: await read_all(
                            call_tool, tools["bill_invoices"], bill_id=bill["id"]
                        )
                        for bill in bills
                    }
                    receipt_ids = list(
                        dict.fromkeys(r for t in transactions for r in t.get("receipt_uuids") or ())
                    )
                    receipts: list[dict[str, Any]] = []
                    for start in range(0, len(receipt_ids), self.receipts_batch):
                        batch = receipt_ids[start : start + self.receipts_batch]
                        receipts += await read_all(
                            call_tool, tools["receipts"], receipt_uuids=batch
                        )
                    users = await read_all(call_tool, tools["users"])
                    chart = tools["org_chart"]
                    org_charts = {
                        user["id"]: await call_tool(
                            chart.name, {**chart.arguments, "user_id": user["id"]}
                        )
                        for user in users
                    }
                    return RampPayloads(
                        transactions=transactions,
                        bills=bills,
                        bill_invoices=bill_invoices,
                        receipts=receipts,
                        users=users,
                        org_charts=org_charts,
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
    """The calendar date of an ISO 8601 date or datetime, in the offset Ramp wrote it
    in. Ramp writes card times in UTC; they are not moved to a local time zone."""
    if not value:
        return None
    return datetime.fromisoformat(value).date() if "T" in value else date.fromisoformat(value)


# The query string and fragment of a URL, where presigned URLs keep their signature.
_URL_QUERY = re.compile(r"(https?://[^\s?#'\"]+)[?#][^\s'\"]*")


def _unsigned(value: Any) -> Any:
    """`value` with the query string and fragment cut from every URL in any string,
    at any depth. Ramp's file links are presigned and the signature is a credential,
    so `raw` keeps scheme, host and path (enough to identify the file) and never the
    signature. Fields are kept, not dropped, so `raw` still has the original's shape."""
    if isinstance(value, str):
        return _URL_QUERY.sub(r"\1", value)
    if isinstance(value, Mapping):
        return {key: _unsigned(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_unsigned(item) for item in value]
    return value


def decimal_usd_cents(value: Any, currency: Any, what: str) -> int:
    """A card transaction's `amount_value` ("431.41", major units, as a decimal string)
    in exact integer cents. No floats, no rounding, and no currency conversion: an
    amount not settled in USD is refused (the PRD covers USD only)."""
    if currency != "USD":
        raise RampMappingError(f"{what}: settled in {currency}, not USD")
    if not isinstance(value, str):
        raise RampMappingError(f"{what}: amount {value!r} is not a decimal string")
    try:
        cents = Decimal(value) * 100
    except InvalidOperation:
        raise RampMappingError(f"{what}: amount {value!r} is not a number") from None
    if not cents.is_finite() or cents != cents.to_integral_value():
        raise RampMappingError(f"{what}: amount {value!r} is not a whole number of cents")
    return int(cents)


def _usd_cents(money: Mapping[str, Any] | None, what: str) -> int:
    """Ramp's Developer API CurrencyAmount {amount, currency_code}, in cents. Bills only;
    the MCP bill shape is unconfirmed."""
    if not money:
        raise RampMappingError(f"{what}: no amount")
    currency = money.get("currency") or money.get("currency_code")
    value = money["value"] if "value" in money else money.get("amount")
    if currency != "USD" or money.get("minor_unit_conversion_rate", 100) != 100:
        raise RampMappingError(f"{what}: amount is not in US cents ({currency})")
    if not isinstance(value, int):
        raise RampMappingError(f"{what}: amount {value!r} is not an integer")
    return value


def _gl_account(selections: Iterable[Any] | None) -> str | None:
    """The one GL account among accounting selections, in the Developer API's shape.
    Unconfirmed on the MCP server: the sandbox has no accounting provider, so its
    `accounting_categories` were null."""
    names = {
        selection.get("name")
        for selection in selections or ()
        if isinstance(selection, Mapping)
        and (selection.get("category_info") or {}).get("type") == "GL_ACCOUNT"
    }
    names.discard(None)
    return names.pop() if len(names) == 1 else None


def map_card_transaction(payload: Mapping[str, Any]) -> SpendTransaction:
    """A `ramp_get_transactions` record. Ramp's MCP server exposes no approvals for it,
    and no department: both stay empty (see APPROVALS_UNAVAILABLE)."""
    ramp_id = payload["transaction_uuid"]
    what = f"transaction {ramp_id}"
    vendor = payload.get("merchant_name")
    purchased = _day(payload.get("transaction_time"))
    if not vendor or purchased is None:
        raise RampMappingError(f"{what}: no merchant name or no transaction time")
    spender = payload.get("spent_by_user_uuid")
    return SpendTransaction(
        id=transaction_id(ramp_id),
        kind=TransactionKind.CARD_EXPENSE,
        source=SourceRef(system=SourceSystem.RAMP, record_type="transaction", record_id=ramp_id),
        vendor_name=vendor,
        amount_cents=decimal_usd_cents(
            payload.get("amount_value"), payload.get("amount_currency"), what
        ),
        transaction_date=purchased,
        accounting_date=_day(payload.get("accounting_date")),
        paid_date=_day(payload.get("settlement_date")),
        memo=payload.get("reason_or_justification") or None,
        # The books' category, not Ramp's merchant category (`merchant_category`, in raw).
        category=_gl_account(payload.get("accounting_categories")),
        spender_id=person_id(spender) if spender else None,
        document_ids=[receipt_document_id(r) for r in payload.get("receipt_uuids") or ()],
        raw=_unsigned(payload),
    )


def map_bill(
    payload: Mapping[str, Any], invoices: Iterable[Mapping[str, Any]] = ()
) -> SpendTransaction:
    """A bill in the Developer API's shape (unconfirmed: the sandbox has no bills), with
    the attachments `ramp_get_bill_invoices` returned for it."""
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
        document_ids=[attachment_document_id(a["id"]) for a in invoices],
        raw=_unsigned(payload),
    )


def managers(org_charts: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Ramp user id -> their manager's, read from each user's `direct_reports`."""
    found: dict[str, str] = {}
    for manager, chart in org_charts.items():
        for report in chart.get("direct_reports") or ():
            report_id = report.get("id")
            if report_id in found and found[report_id] != manager:
                raise RampMappingError(f"user {report_id} is a direct report of two managers")
            if report_id:
                found[report_id] = manager
    return found


def map_user(payload: Mapping[str, Any], manager: str | None = None) -> Person:
    """A `ramp_list_users` record. `manager` is the Ramp id from the org chart."""
    name = " ".join(p for p in (payload.get("first_name"), payload.get("last_name")) if p)
    return Person(
        id=person_id(payload["id"]),
        name=name or payload.get("email") or payload["id"],
        email=payload.get("email") or None,
        # Ramp's `role` is a permission level (BUSINESS_USER, ...), not a job role,
        # so it is not what approval rules match on. Ramp holds no job title.
        role=None,
        department=payload.get("department_name") or None,
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
    """Receipt and invoice files to download. Their shapes are unconfirmed (the sandbox
    has none); a record without a file URL is refused, not skipped."""
    files = []
    for receipt in payloads.receipts:
        if not receipt.get("receipt_url"):
            raise RampUnavailable(f"receipt {receipt['id']}: Ramp returned no file URL")
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
    for attachments in payloads.bill_invoices.values():
        for attachment in attachments:
            if not attachment.get("url"):
                raise RampUnavailable(f"bill attachment {attachment['id']}: no file URL")
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
        # Card transactions left out, by reason. `describe()` reports them.
        self.left_out: Counter[str] = Counter()
        self._transactions = []
        for payload in payloads.transactions:
            state = str(payload.get("state") or "").upper()
            if state in SKIPPED_STATES:
                continue
            if state in PENDING_STATES:
                self.left_out["pending"] += 1
            elif payload.get("amount_currency") != "USD":
                self.left_out["not settled in USD"] += 1
            else:
                self._transactions.append(map_card_transaction(payload))
        self._transactions += [
            map_bill(b, payloads.bill_invoices.get(b["id"], ()))
            for b in payloads.bills
            if b.get("status") not in SKIPPED_BILL_STATUSES
            and b.get("status_summary") not in SKIPPED_BILL_SUMMARIES
        ]
        reports_to = managers(payloads.org_charts)
        self._people = [map_user(u, reports_to.get(u["id"])) for u in payloads.users]
        self._attach_documents(remote_files(payloads), download)

    def _attach_documents(self, files: list[RemoteFile], download: Downloader) -> None:
        """Downloads `files`, and refuses a transaction naming a file Ramp did not return."""
        self._documents = {d.id: d for d in self._download(files, download)}
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

    def notes(self) -> list[str]:
        """What a reader of `describe()` must know about these records."""
        left_out = [f"{n} {why}" for why, n in sorted(self.left_out.items())]
        return [APPROVALS_UNAVAILABLE] + (
            [f"card transactions left out: {', '.join(left_out)}"] if left_out else []
        )

    def describe(self) -> list[SourceUse]:
        notes = "; ".join(self.notes())
        if self.is_standin:
            served = self.server.get("description") or "dataset not stated"
            ramp = SourceUse(
                system=SourceSystem.RAMP,
                mode="local",
                detail=f"Ramp stand-in MCP server at {self.endpoint}, {served}; not Ramp; {notes}",
            )
        else:
            ramp = SourceUse(
                system=SourceSystem.RAMP,
                mode="live",
                detail=f"Ramp MCP {self.endpoint}; {notes}",
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
