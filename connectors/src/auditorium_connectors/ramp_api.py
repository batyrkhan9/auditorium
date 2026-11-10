"""Ramp, read through its Developer API (REST, client credentials), behind `Sources`.

The unattended path: no browser and no OAuth login, only an app's client id and
secret. It is the only Ramp path that reaches approvals for card transactions
(`GET /developer/v1/approvals`); the MCP server exposes none.

- `RampApiClient` gets a token with the client-credentials grant, held in memory
  for this process only, and reads every page of transactions, bills, receipts,
  users, departments and approvals.
- `ApiRampSources` maps those payloads to contract records. It reuses the MCP
  path's bill mapping, money checks, URL scrubbing and file download from `ramp.py`.

Payload shapes follow Ramp's OpenAPI spec (docs.ramp.com/openapi/developer-api.json,
read 2026-11-10). None has been captured from Ramp yet; see
connectors/decisions/2026-11-10-ramp-developer-api.md.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from auditorium_contracts import (
    Approval,
    LocalSources,
    SourceRef,
    Sources,
    SourceSystem,
    SourceUse,
    SpendTransaction,
    TransactionKind,
)

from .fallback import LocalFallback
from .ramp import (
    MAX_PAGES,
    PENDING_STATES,
    SKIPPED_BILL_STATUSES,
    SKIPPED_BILL_SUMMARIES,
    SKIPPED_STATES,
    Downloader,
    RampMappingError,
    RampPayloads,
    RampSources,
    RampUnavailable,
    ToolCall,
    _counted,
    _day,
    _gl_account,
    _reason,
    _unsigned,
    _usd_cents,
    http_download,
    map_bill,
    map_user,
    person_id,
    receipt_document_id,
    records_from,
    remote_files,
    transaction_id,
)

API_URL = "https://demo-api.ramp.com"  # the sandbox; production is https://api.ramp.com
TOKEN_PATH = "/developer/v1/token"
APPROVALS_PATH = "/developer/v1/approvals"
# Without these the read is incomplete, so the connection fails.
REQUIRED_SCOPES = (
    "transactions:read",
    "bills:read",
    "receipts:read",
    "users:read",
    "departments:read",
)
# Without this, approvals are reported unavailable and stay empty.
APPROVALS_SCOPE = "approvals:read"
PAGE_SIZE = 100  # the spec's maximum
APPROVALS_BATCH = 50  # entity ids per approvals request
# Card transactions not yet initiated are pending too.
API_PENDING_STATES = PENDING_STATES | {"PENDING_INITIATION"}
# The approvals endpoint answers these when the scope or the plan feature is missing.
APPROVALS_REFUSALS = {403, 404}
# Step statuses that are a decision. Others (the spec does not list them) are counted.
DECISIONS = {"APPROVED": "approved", "REJECTED": "rejected"}


class RampApiError(RampUnavailable):
    """Ramp answered an HTTP error."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _redact(text: str, secrets: Iterable[str | None]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


@dataclass(frozen=True)
class ApiPayloads:
    """Everything read from the Developer API, as Ramp returned it."""

    transactions: list[dict[str, Any]]
    bills: list[dict[str, Any]]
    receipts: list[dict[str, Any]]
    users: list[dict[str, Any]]
    departments: list[dict[str, Any]]
    # `ApprovalsResource` records. Empty when unavailable; then the reason is set.
    approvals: list[dict[str, Any]] = field(default_factory=list)
    approvals_unavailable: str | None = None


class RampApiClient:
    """Reads Ramp's Developer API with a client-credentials token kept in memory only."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = API_URL,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.Client(base_url=self.base_url, transport=transport, timeout=60)
        self._token: str | None = None
        # Why approvals cannot be read, when the token says so before any call.
        self.approvals_unavailable: str | None = None

    def __repr__(self) -> str:
        return f"RampApiClient(base_url={self.base_url!r})"

    def _scrub(self, text: str) -> str:
        return _redact(text, (self._client_secret, self._client_id, self._token))

    def _request_token(self, scopes: Iterable[str]) -> httpx.Response:
        return self._http.post(
            TOKEN_PATH,
            auth=(self._client_id, self._client_secret),
            data={"grant_type": "client_credentials", "scope": " ".join(scopes)},
            headers={"Accept": "application/json"},
        )

    def token(self) -> str:
        """A token for the required scopes and, if the app allows it, approvals:read."""
        if self._token:
            return self._token
        response = self._request_token((*REQUIRED_SCOPES, APPROVALS_SCOPE))
        if response.status_code == 400:
            # An app without approvals:read is refused the whole request. Ask without it.
            response = self._request_token(REQUIRED_SCOPES)
            if response.status_code == 200:
                self.approvals_unavailable = (
                    f"the token request with {APPROVALS_SCOPE} was refused (HTTP 400)"
                )
        if response.status_code != 200:
            raise RampApiError(
                f"token request refused: HTTP {response.status_code} "
                f"{self._scrub(response.text[:200])}",
                response.status_code,
            )
        body = response.json()
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise RampUnavailable("token response carries no access_token")
        self._token = token
        granted = body.get("scope")
        if isinstance(granted, str):
            scopes = set(granted.split())
            missing = [scope for scope in REQUIRED_SCOPES if scope not in scopes]
            if missing:
                raise RampUnavailable(f"token lacks scopes {missing}; add them to the Ramp app")
            if APPROVALS_SCOPE not in scopes and not self.approvals_unavailable:
                self.approvals_unavailable = f"the token was not granted {APPROVALS_SCOPE}"
        return token

    def _get(self, path: str, params: httpx.QueryParams) -> Any:
        response = self._http.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/json"},
        )
        if response.status_code != 200:
            raise RampApiError(
                f"GET {path}: HTTP {response.status_code} {self._scrub(response.text[:200])}",
                response.status_code,
            )
        try:
            return response.json()
        except ValueError:
            raise RampUnavailable(f"GET {path} did not return JSON") from None

    def get_all(self, path: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Every record of a list endpoint. Follows `page.next`, but always on our own
        host: only its query is used, so the token never goes anywhere else. Refuses a
        partial read: another "more" marker, fewer records than `page.total_count`, a
        repeated page or more than MAX_PAGES pages."""
        call = ToolCall(f"GET {path}", records_key="data", cursor_field="next")
        query = httpx.QueryParams({**params, "page_size": PAGE_SIZE})
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        total = 0
        for _ in range(MAX_PAGES):
            payload = self._get(path, query)
            page = records_from(payload, call)
            records.extend(page)
            info = payload.get("page")
            info = info if isinstance(info, dict) else {}
            total = max(total, _counted(info), _counted(payload))
            following = info.get("next")
            if not following or not page:
                if total > len(records):
                    raise RampUnavailable(f"GET {path} returned {len(records)} of {total} records")
                return records
            if not isinstance(following, str) or following in seen:
                raise RampUnavailable(f"GET {path} repeated a page")
            seen.add(following)
            query = query.merge(httpx.URL(following).params)
        raise RampUnavailable(f"GET {path} gave more than {MAX_PAGES} pages")

    def _approvals(self, entity_type: str, ids: list[str]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for start in range(0, len(ids), APPROVALS_BATCH):
            batch = ids[start : start + APPROVALS_BATCH]
            found += self.get_all(APPROVALS_PATH, {"entity_type": entity_type, "entity_ids": batch})
        return found

    def fetch(self) -> ApiPayloads:
        self.token()
        transactions = self.get_all(
            "/developer/v1/transactions", {"state": "ALL", "include_count": "true"}
        )
        bills = self.get_all("/developer/v1/bills", {})
        receipts = self.get_all("/developer/v1/receipts", {})
        # The default leaves out suspended users, who may still have approved something.
        users = self.get_all("/developer/v1/users", {})
        users += self.get_all("/developer/v1/users", {"status": "USER_SUSPENDED"})
        users = list({user["id"]: user for user in users}.values())
        departments = self.get_all("/developer/v1/departments", {})
        approvals: list[dict[str, Any]] = []
        unavailable = self.approvals_unavailable
        if not unavailable:
            card_ids = [
                t["id"]
                for t in transactions
                if str(t.get("state") or "").upper() not in SKIPPED_STATES
            ]
            try:
                approvals = self._approvals("TRANSACTION", card_ids)
                approvals += self._approvals("BILL", [b["id"] for b in bills])
            except RampApiError as error:
                if error.status not in APPROVALS_REFUSALS:
                    raise
                approvals, unavailable = [], f"GET {APPROVALS_PATH} answered HTTP {error.status}"
        return ApiPayloads(
            transactions=transactions,
            bills=bills,
            receipts=receipts,
            users=users,
            departments=departments,
            approvals=approvals,
            approvals_unavailable=unavailable,
        )


# --- mapping ------------------------------------------------------------------


def map_api_card_transaction(payload: Mapping[str, Any]) -> SpendTransaction:
    """A Developer API `Transaction`. Department stays empty, as on the MCP path:
    `card_holder.department_name` is the cardholder's department now, not at purchase."""
    ramp_id = payload["id"]
    what = f"transaction {ramp_id}"
    vendor = payload.get("merchant_name") or payload.get("merchant_descriptor")
    purchased = _day(payload.get("user_transaction_time"))
    if not vendor or purchased is None:
        raise RampMappingError(f"{what}: no merchant name or no transaction time")
    if payload.get("minor_unit_conversion_rate") not in (None, 100):
        raise RampMappingError(f"{what}: amount is not in cents")
    holder = payload.get("card_holder") or {}
    return SpendTransaction(
        id=transaction_id(ramp_id),
        kind=TransactionKind.CARD_EXPENSE,
        source=SourceRef(system=SourceSystem.RAMP, record_type="transaction", record_id=ramp_id),
        vendor_name=vendor,
        # Signed integer minor units: a refund is negative, as the contract wants.
        amount_cents=_usd_cents(payload.get("entity_amount"), what),
        transaction_date=purchased,
        accounting_date=_day(payload.get("accounting_date")),
        paid_date=_day(payload.get("settlement_date")),
        memo=payload.get("memo") or None,
        category=_gl_account(payload.get("accounting_field_selections")),
        spender_id=person_id(holder["user_id"]) if holder.get("user_id") else None,
        document_ids=[receipt_document_id(r) for r in payload.get("receipts") or ()],
        raw=_unsigned(payload),
    )


def map_approvals(resource: Mapping[str, Any], unmapped: Counter[str]) -> list[Approval]:
    """The decisions in an `ApprovalsResource` history. A step that is not a decision,
    or that names no user, is left out and counted in `unmapped`: never invented."""
    approvals = []
    for step in resource.get("approval_history") or ():
        status = str(step.get("step_status") or "")
        decision = DECISIONS.get(status.upper())
        if decision is None:
            unmapped[f"step status {status or 'missing'}"] += 1
        elif not step.get("user_id"):
            unmapped[f"{decision} step without a user"] += 1
        else:
            approvals.append(
                Approval(
                    approver_id=person_id(step["user_id"]),
                    decision=decision,
                    decided_at=datetime.fromisoformat(step["action_at"]),
                )
            )
    return approvals


def _is_usd(money: Any) -> bool:
    return (
        isinstance(money, Mapping)
        and (money.get("currency") or money.get("currency_code")) == "USD"
    )


class ApiRampSources(RampSources):
    """Card expenses, bills, their files, people and approvals live from the Developer
    API; ledger, bank and policy from `local`. Everything is read when it is built."""

    def __init__(
        self,
        payloads: ApiPayloads,
        local: Sources,
        *,
        endpoint: str,
        cache_dir: Path | str,
        download: Downloader = http_download,
    ):
        self.endpoint = endpoint
        self.local = local
        self.cache_dir = Path(cache_dir)
        self.server = {}
        self.left_out: Counter[str] = Counter()
        self.bills_left_out: Counter[str] = Counter()
        self.approvals_unavailable = payloads.approvals_unavailable
        self.unmapped_steps: Counter[str] = Counter()
        self.chain_statuses: Counter[str] = Counter()

        chains: dict[tuple[str, str], Mapping[str, Any]] = {}
        for resource in payloads.approvals:
            key = (str(resource.get("entity_type")).lower(), resource["entity_id"])
            if key in chains:
                raise RampMappingError(f"two approval chains for {key[0]} {key[1]}")
            chains[key] = resource

        def approvals(kind: str, ramp_id: str) -> list[Approval]:
            chain = chains.get((kind, ramp_id))
            if chain is None:
                return []
            self.chain_statuses[str(chain.get("status"))] += 1
            return map_approvals(chain, self.unmapped_steps)

        self._transactions = []
        for payload in payloads.transactions:
            state = str(payload.get("state") or "").upper()
            if state in SKIPPED_STATES:
                continue
            if state in API_PENDING_STATES:
                self.left_out["pending"] += 1
            elif payload.get("entity_amount") is None:
                self.left_out["with no settled amount"] += 1
            elif not _is_usd(payload.get("entity_amount")):
                self.left_out["not settled in USD"] += 1
            else:
                txn = map_api_card_transaction(payload)
                self._transactions.append(
                    txn.model_copy(update={"approvals": approvals("transaction", payload["id"])})
                )
        kept_bills = []
        for bill in payloads.bills:
            if (
                bill.get("status") in SKIPPED_BILL_STATUSES
                or bill.get("status_summary") in SKIPPED_BILL_SUMMARIES
            ):
                continue
            if not _is_usd(bill.get("amount")):
                self.bills_left_out["not in USD"] += 1
                continue
            txn = map_bill(bill, bill.get("attachments") or ())
            self._transactions.append(
                txn.model_copy(update={"approvals": approvals("bill", bill["id"])})
            )
            kept_bills.append(bill)

        names = {d["id"]: d.get("name") for d in payloads.departments}
        self._people = [
            map_user(
                {**u, "department_name": names.get(u.get("department_id") or "")},
                u.get("manager_id"),
            )
            for u in payloads.users
        ]
        # Only receipts on transactions we keep are downloaded.
        wanted = {d for t in self._transactions for d in t.document_ids}
        files = remote_files(
            RampPayloads(
                transactions=[],
                bills=[],
                bill_invoices={b["id"]: b.get("attachments") or [] for b in kept_bills},
                receipts=[r for r in payloads.receipts if receipt_document_id(r["id"]) in wanted],
                users=[],
                org_charts={},
            )
        )
        self._attach_documents(files, download)

    def notes(self) -> list[str]:
        if self.approvals_unavailable:
            notes = [
                f"approvals unavailable ({self.approvals_unavailable}), so an empty "
                "approvals list means unknown, not unapproved"
            ]
        else:
            notes = [
                f"approvals from {APPROVALS_PATH}; a record with no approval chain there "
                "has empty approvals"
            ]
            unfinished = {s: n for s, n in self.chain_statuses.items() if s != "APPROVED"}
            if unfinished:
                notes.append(
                    "approval chains not approved: "
                    + ", ".join(f"{n} {s}" for s, n in sorted(unfinished.items()))
                )
            if self.unmapped_steps:
                notes.append(
                    "approval steps left out: "
                    + ", ".join(f"{n} {s}" for s, n in sorted(self.unmapped_steps.items()))
                )
        for what, counter in (("card transactions", self.left_out), ("bills", self.bills_left_out)):
            if counter:
                left = ", ".join(f"{n} {why}" for why, n in sorted(counter.items()))
                notes.append(f"{what} left out: {left}")
        return notes

    def describe(self) -> list[SourceUse]:
        ramp = SourceUse(
            system=SourceSystem.RAMP,
            mode="live",
            detail=f"Ramp Developer API at {httpx.URL(self.endpoint).host}; "
            + "; ".join(self.notes()),
        )
        return [ramp] + [
            use for use in self.local.describe() if use.system is not SourceSystem.RAMP
        ]


def connect_ramp_api(
    local: LocalSources,
    *,
    client_id: str | None,
    client_secret: str | None,
    base_url: str = API_URL,
    cache_dir: Path | str,
    client: RampApiClient | None = None,
    download: Downloader = http_download,
) -> Sources:
    """Live Ramp through the Developer API if it loads completely, otherwise the local
    dataset with the reason why. No reason ever carries the secret."""
    host = httpx.URL(base_url).host or base_url
    if not client_id or not client_secret:
        return LocalFallback(
            local, {SourceSystem.RAMP: "RAMP_CLIENT_ID and RAMP_CLIENT_SECRET are not both set"}
        )
    if httpx.URL(base_url).scheme != "https":
        return LocalFallback(
            local, {SourceSystem.RAMP: f"RAMP_API_URL must be https, not {base_url}"}
        )
    try:
        client = client or RampApiClient(client_id, client_secret, base_url)
        return ApiRampSources(
            client.fetch(), local, endpoint=base_url, cache_dir=cache_dir, download=download
        )
    except Exception as error:
        reason = _redact(_reason(error), (client_secret, client_id))
        return LocalFallback(
            local, {SourceSystem.RAMP: f"Ramp Developer API at {host} failed: {reason}"}
        )
