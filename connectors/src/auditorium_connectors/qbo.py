"""QuickBooks Online, through its REST Accounting API, behind the contract.

Reads Bills, Purchases (card and cash expenses) and JournalEntries and maps them
to `LedgerEntry`. Loads a dataset's `ledger.jsonl` into a sandbox company.
Why REST and not Intuit's MCP server: connectors/decisions/2026-11-09-quickbooks-access.md.

Configuration comes from the environment only:

    QBO_ENVIRONMENT    sandbox | production (loading refuses anything but sandbox)
    QBO_CLIENT_ID      Intuit developer app keys
    QBO_CLIENT_SECRET
    QBO_REALM_ID       the company id
    QBO_REFRESH_TOKEN  from the OAuth 2.0 Playground or an auth-code flow
    QBO_TOKEN_CACHE    optional; where a rotated refresh token is kept
                       (default ~/.cache/auditorium/qbo_token.json, outside the repo)

    python -m auditorium_connectors.qbo fetch [--out ledger.jsonl] [--fallback DATASET]
    python -m auditorium_connectors.qbo load DATASET [--dry-run]
    python -m auditorium_connectors.qbo repair-types DATASET [--write]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

import httpx
from auditorium_contracts import (
    LedgerEntry,
    LedgerEntryType,
    LedgerLine,
    LocalSources,
    SourceRef,
    SourceSystem,
    SourceUse,
    read_jsonl,
    write_jsonl,
)

BASE_URLS = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
# Intuit treats every minor version below 75 as 75 since August 2025.
MINOR_VERSION = "75"
PAGE_SIZE = 1000
REQUIRED_ENV = (
    "QBO_ENVIRONMENT",
    "QBO_CLIENT_ID",
    "QBO_CLIENT_SECRET",
    "QBO_REALM_ID",
    "QBO_REFRESH_TOKEN",
)
DEFAULT_TOKEN_CACHE = Path.home() / ".cache" / "auditorium" / "qbo_token.json"

# The loader stamps this line into PrivateNote so a second load finds what the
# first one created. Mapping strips it from `memo`; `raw` keeps it.
REF_PREFIX = "auditorium-ref: "

ENTITY_TYPES = {
    "Bill": LedgerEntryType.BILL,
    "Purchase": LedgerEntryType.EXPENSE,
    "JournalEntry": LedgerEntryType.JOURNAL_ENTRY,
}
QBO_TYPE_FOR = {entry_type: name for name, entry_type in ENTITY_TYPES.items()}


class QboError(RuntimeError):
    """QuickBooks could not be reached or refused a request. Never carries a secret."""


class QboConfigError(QboError):
    pass


@dataclass(frozen=True)
class QboConfig:
    environment: str
    client_id: str
    client_secret: str = field(repr=False)
    realm_id: str
    refresh_token: str = field(repr=False)
    token_cache: Path | None = DEFAULT_TOKEN_CACHE

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> QboConfig:
        missing = [name for name in REQUIRED_ENV if not env.get(name, "").strip()]
        if missing:
            raise QboConfigError(f"QuickBooks is not configured: {', '.join(missing)} not set")
        environment = env["QBO_ENVIRONMENT"].strip().lower()
        if environment not in BASE_URLS:
            raise QboConfigError(
                f"QBO_ENVIRONMENT must be 'sandbox' or 'production', not {environment!r}"
            )
        cache = env.get("QBO_TOKEN_CACHE", "").strip()
        return cls(
            environment=environment,
            client_id=env["QBO_CLIENT_ID"].strip(),
            client_secret=env["QBO_CLIENT_SECRET"].strip(),
            realm_id=env["QBO_REALM_ID"].strip(),
            refresh_token=env["QBO_REFRESH_TOKEN"].strip(),
            token_cache=Path(cache).expanduser() if cache else DEFAULT_TOKEN_CACHE,
        )

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.environment]


class QboClient:
    """A thin client for the QBO Accounting API: refresh, query, create."""

    def __init__(self, config: QboConfig, http: httpx.Client | None = None):
        self.config = config
        self.http = http or httpx.Client(timeout=30.0)
        self._access_token: str | None = None
        self._refresh_token = self._cached_refresh_token() or config.refresh_token
        self._account_types: dict[str, str] | None = None

    # Intuit rotates refresh tokens and the old one stops working, so the newest
    # one is kept outside the repo, tied to the env token it descends from. A new
    # QBO_REFRESH_TOKEN in .env makes the cache stale and it is ignored.
    def _seed(self) -> str:
        return hashlib.sha256(self.config.refresh_token.encode()).hexdigest()

    def _cached_refresh_token(self) -> str | None:
        cache = self.config.token_cache
        try:
            data = json.loads(cache.read_text("utf-8")) if cache else None
        except (OSError, ValueError):
            return None
        if isinstance(data, dict) and data.get("seed") == self._seed():
            return data.get("refresh_token")
        return None

    def _store_refresh_token(self, token: str) -> None:
        cache = self.config.token_cache
        if not cache:
            return
        # Written to a fresh file that is 0600 from creation, then moved into place,
        # so the token is never readable by others and never half written.
        tmp = cache.with_name(f".{cache.name}.{secrets.token_hex(8)}.tmp")
        try:
            cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(json.dumps({"seed": self._seed(), "refresh_token": token}))
            os.replace(tmp, cache)
        except OSError as error:
            tmp.unlink(missing_ok=True)
            print(f"warning: could not keep the rotated refresh token: {error}", file=sys.stderr)

    def _refresh(self) -> None:
        response = self.http.post(
            TOKEN_URL,
            auth=(self.config.client_id, self.config.client_secret),
            data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            raise QboError(
                f"token refresh failed: HTTP {response.status_code} {_oauth_error(response)}"
            )
        body = response.json()
        self._access_token = body["access_token"]
        new_refresh = body.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            self._store_refresh_token(new_refresh)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if self._access_token is None:
            self._refresh()
        url = f"{self.config.base_url}/v3/company/{self.config.realm_id}/{path}"
        params = {**kwargs.pop("params", {}), "minorversion": MINOR_VERSION}
        for attempt in (1, 2):
            response = self.http.request(
                method,
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                },
                **kwargs,
            )
            if response.status_code == 401 and attempt == 1:
                self._refresh()  # access tokens last an hour
                continue
            break
        if response.status_code >= 400:
            raise QboError(f"{method} {path}: HTTP {response.status_code} {_fault(response)}")
        return response.json()

    def query(self, entity: str) -> list[dict[str, Any]]:
        """Every record of one entity type, paged."""
        records: list[dict[str, Any]] = []
        start = 1
        while True:
            statement = f"SELECT * FROM {entity} STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}"
            body = self._request("GET", "query", params={"query": statement})
            page = body.get("QueryResponse", {}).get(entity, [])
            records.extend(page)
            if len(page) < PAGE_SIZE:
                return records
            start += PAGE_SIZE

    def create(self, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = self._request("POST", entity.lower(), json=payload)
        return body[entity]

    def update(self, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        """QuickBooks updates by POSTing a body with Id and SyncToken to the create path."""
        return self.create(entity, payload)

    def account_types(self) -> dict[str, str]:
        """Account id, and account name, -> its QuickBooks AccountType. Read once per client."""
        if self._account_types is None:
            types: dict[str, str] = {}
            for account in self.query("Account"):
                kind = account.get("AccountType")
                if not kind:
                    continue
                types[f"id:{account['Id']}"] = kind
                for name in (account.get("Name"), account.get("FullyQualifiedName")):
                    if name:
                        types.setdefault(f"name:{name}", kind)
            self._account_types = types
        return self._account_types

    def fetch_ledger(self) -> list[LedgerEntry]:
        """Every Bill, Purchase and JournalEntry. Other entity types are not read."""
        types = self.account_types()
        entries: list[LedgerEntry] = []
        for entity in ENTITY_TYPES:
            entries.extend(map_entity(entity, record, types) for record in self.query(entity))
        return entries


def _oauth_error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("error", "")) if isinstance(body, dict) else ""


def _fault(response: httpx.Response) -> str:
    try:
        errors = response.json().get("Fault", {}).get("Error", [])
    except (ValueError, AttributeError):
        return response.text[:300]
    return "; ".join(f"{e.get('Message', '')}: {e.get('Detail', '')}" for e in errors)


# --- QBO payload -> LedgerEntry ------------------------------------------------


def to_cents(amount: Any) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def to_dollars(cents: int) -> float:
    return float(Decimal(cents) / 100)


def _name(ref: Any) -> str | None:
    return ref.get("name") if isinstance(ref, dict) else None


def _memo(private_note: str | None) -> str | None:
    if not private_note:
        return None
    kept = [line for line in private_note.splitlines() if not line.startswith(REF_PREFIX)]
    text = "\n".join(kept).strip()
    return text or None


def loaded_ref(record: dict[str, Any]) -> str | None:
    """The dataset ledger id the loader stamped on a QBO record, if any."""
    for line in (record.get("PrivateNote") or "").splitlines():
        if line.startswith(REF_PREFIX):
            return line[len(REF_PREFIX) :].strip()
    return None


def _account_type(ref: Any, types: Mapping[str, str]) -> str | None:
    """The AccountType of the account `ref` points at: by id, else by name."""
    if not isinstance(ref, dict):
        return None
    return types.get(f"id:{ref.get('value')}") or types.get(f"name:{ref.get('name')}")


def _expense_lines(record: dict[str, Any], side: str, types: Mapping[str, str]) -> list[LedgerLine]:
    """Account-based lines. Item-based lines name an item, not an account, so they
    are left out here and stay in `raw`. QuickBooks sets no department on these
    lines (only on the entry), so they carry none."""
    lines = []
    for line in record.get("Line", []):
        detail = line.get("AccountBasedExpenseLineDetail")
        if line.get("DetailType") != "AccountBasedExpenseLineDetail" or not detail:
            continue
        account = _name(detail.get("AccountRef"))
        if not account:
            continue
        lines.append(
            LedgerLine(
                account=account,
                account_type=_account_type(detail.get("AccountRef"), types),
                side=side,
                amount_cents=abs(to_cents(line.get("Amount", 0))),
                description=line.get("Description"),
            )
        )
    return lines


def _balancing_line(ref: Any, side: str, cents: int, types: Mapping[str, str]):
    account = _name(ref)
    if not account:
        return []
    return [
        LedgerLine(
            account=account,
            account_type=_account_type(ref, types),
            side=side,
            amount_cents=abs(cents),
        )
    ]


def map_entity(
    entity: str, record: dict[str, Any], account_types: Mapping[str, str] | None = None
) -> LedgerEntry:
    """One QBO Bill, Purchase or JournalEntry as a `LedgerEntry`, original in `raw`.

    Bills and Purchases list only their expense lines; the credit side is the
    header's APAccountRef or AccountRef, which QuickBooks posts the total to. It
    becomes the balancing line when the payload names it. `account_types` (from
    `QboClient.account_types`) fills each line's `account_type`; an account not in
    it gets none. The entry's DepartmentRef goes on the entry; only a journal line's
    own DepartmentRef goes on that line.
    """
    if entity not in ENTITY_TYPES:
        raise ValueError(f"not a ledger entity: {entity}")
    types = account_types or {}
    total = to_cents(record.get("TotalAmt", 0))
    department = _name(record.get("DepartmentRef"))
    vendor: str | None = None

    if entity == "Bill":
        vendor = _name(record.get("VendorRef"))
        lines = _expense_lines(record, "debit", types) + _balancing_line(
            record.get("APAccountRef"), "credit", total, types
        )
    elif entity == "Purchase":
        entity_ref = record.get("EntityRef") or {}
        if entity_ref.get("type") in (None, "Vendor"):
            vendor = _name(entity_ref)
        # Credit=true is a card refund: the expense is credited, the card debited.
        refund = bool(record.get("Credit"))
        if refund:
            total = -total
        expense_side, card_side = ("credit", "debit") if refund else ("debit", "credit")
        lines = _expense_lines(record, expense_side, types) + _balancing_line(
            record.get("AccountRef"), card_side, total, types
        )
    else:
        lines, vendors = [], set()
        for line in record.get("Line", []):
            detail = line.get("JournalEntryLineDetail")
            if line.get("DetailType") != "JournalEntryLineDetail" or not detail:
                continue
            account = _name(detail.get("AccountRef"))
            if not account:
                continue
            entity_info = detail.get("Entity") or {}
            line_vendor = None
            if entity_info.get("Type") == "Vendor":
                line_vendor = _name(entity_info.get("EntityRef"))
            if line_vendor:
                vendors.add(line_vendor)
            lines.append(
                LedgerLine(
                    account=account,
                    account_type=_account_type(detail.get("AccountRef"), types),
                    side="debit" if detail.get("PostingType") == "Debit" else "credit",
                    amount_cents=abs(to_cents(line.get("Amount", 0))),
                    description=line.get("Description"),
                    vendor_name=line_vendor,
                    department=_name(detail.get("DepartmentRef")),
                )
            )
        # A journal entry has no header vendor. When its lines name exactly one,
        # that is the vendor it was booked for.
        vendor = vendors.pop() if len(vendors) == 1 else None
        # QuickBooks sends TotalAmt 0 on every journal entry (seen live, #36), so
        # the total is always the sum of the debit lines, never the header.
        total = sum(line.amount_cents for line in lines if line.side == "debit")

    record_id = str(record["Id"])
    return LedgerEntry(
        id=f"qbo:{entity}:{record_id}",
        source=SourceRef(system=SourceSystem.QBO, record_type=entity, record_id=record_id),
        entry_type=ENTITY_TYPES[entity],
        txn_date=date.fromisoformat(record["TxnDate"]),
        vendor_name=vendor,
        doc_number=record.get("DocNumber") or None,
        memo=_memo(record.get("PrivateNote")),
        department=department,
        payment_type=record.get("PaymentType") if entity == "Purchase" else None,
        total_cents=total,
        lines=lines,
        raw=record,
    )


# --- LedgerEntry -> QBO payload (the loader) -------------------------------------


class Skip(Exception):
    """This ledger entry cannot be loaded as it is. The message says why."""


# QuickBooks needs an account type to create an account. A v0.2 dataset line carries
# it (`LedgerLine.account_type`, QuickBooks' AccountType names) and that is sent as
# is; only a line without one has its type guessed from its name and role. The
# account an expense was paid from has the type its PaymentType requires, so
# QuickBooks accepts the pair.
PAYMENT_ACCOUNT_TYPES = {"CreditCard": "Credit Card", "Cash": "Bank", "Check": "Bank"}


def guess_account_type(name: str, role: str) -> str:
    if role in PAYMENT_ACCOUNT_TYPES:
        return PAYMENT_ACCOUNT_TYPES[role]
    lowered = name.lower()
    for words, account_type in (
        (("payable",), "Accounts Payable"),
        (("credit card", "card"), "Credit Card"),
        (("checking", "bank", "cash", "savings"), "Bank"),
        (("accrued", "accrual", "liabilit"), "Other Current Liability"),
        (("prepaid",), "Other Current Asset"),
    ):
        if any(word in lowered for word in words):
            return account_type
    return {"ap": "Accounts Payable"}.get(role, "Expense")


def payment_type(entry: LedgerEntry, paid_from: str) -> str:
    """QuickBooks' required Purchase.PaymentType: the entry's `payment_type` (v0.2),
    else CreditCard when the paid-from account is a card account by name. Cash and
    Check cannot be told apart from a name, so Skip. `raw` is not read: it is the
    source's own record, not a format between areas."""
    if entry.payment_type is not None:
        if entry.payment_type not in PAYMENT_ACCOUNT_TYPES:
            raise Skip(
                f"payment_type {entry.payment_type!r} is not one of QuickBooks' "
                f"{sorted(PAYMENT_ACCOUNT_TYPES)}"
            )
        return entry.payment_type
    if guess_account_type(paid_from, "") == "Credit Card":
        return "CreditCard"
    raise Skip(
        f"cannot tell how the expense was paid: {paid_from!r} is not a card account "
        "by name and the entry has no payment_type (Cash or Check)"
    )


@dataclass
class PlannedEntry:
    """One ledger entry ready to send: the QBO entity, and its body with names
    still in place of ids. `accounts` maps each account name to its role, and
    `account_types` to the AccountType the dataset gives it, where it gives one."""

    entry: LedgerEntry
    entity: str
    payload: dict[str, Any]
    accounts: dict[str, str]
    vendor: str | None
    account_types: dict[str, str] = field(default_factory=dict)

    def account_type(self, name: str) -> str:
        """The type to create account `name` with: the dataset's, else a guess."""
        return self.account_types.get(name) or guess_account_type(name, self.accounts[name])


def _line_sum(lines: list[LedgerLine]) -> int:
    return sum(line.amount_cents for line in lines)


def plan_entry(entry: LedgerEntry) -> PlannedEntry:
    """Turn a ledger entry into a QBO create body. Raises Skip when it cannot be
    loaded faithfully, rather than inventing what is missing."""
    entity = QBO_TYPE_FOR.get(entry.entry_type)
    if entity is None:
        raise Skip(f"entry type {entry.entry_type} is not loaded (bills, expenses, journals only)")
    if not entry.lines:
        raise Skip("no lines; QuickBooks needs at least one account line")
    if entry.doc_number and len(entry.doc_number) > 21:
        raise Skip("doc_number is longer than the 21 characters QuickBooks allows")

    note = "\n".join(filter(None, [entry.memo, f"{REF_PREFIX}{entry.id}"]))
    payload: dict[str, Any] = {"TxnDate": entry.txn_date.isoformat(), "PrivateNote": note}
    if entry.doc_number:
        payload["DocNumber"] = entry.doc_number
    accounts: dict[str, str] = {}
    types: dict[str, str] = {}
    for line in entry.lines:
        if not line.account_type:
            continue
        if types.setdefault(line.account, line.account_type) != line.account_type:
            raise Skip(f"account {line.account!r} is given two account types")

    if entity == "JournalEntry":
        debits = [line for line in entry.lines if line.side == "debit"]
        credits = [line for line in entry.lines if line.side == "credit"]
        if _line_sum(debits) != _line_sum(credits):
            raise Skip("debits and credits do not balance")
        payload["Line"] = []
        for line in entry.lines:
            accounts.setdefault(line.account, "journal")
            detail: dict[str, Any] = {
                "PostingType": "Debit" if line.side == "debit" else "Credit",
                "AccountRef": {"name": line.account},
            }
            vendor = line.vendor_name or entry.vendor_name
            if vendor:
                detail["Entity"] = {"Type": "Vendor", "EntityRef": {"name": vendor}}
            payload["Line"].append(_line(line, "JournalEntryLineDetail", detail))
        return PlannedEntry(entry, entity, payload, accounts, entry.vendor_name, types)

    # Bill and Purchase: expense lines on one side, at most one balancing line on
    # the other (the A/P account, or the card or bank account paid from).
    refund = entity == "Purchase" and entry.total_cents < 0
    if entity == "Bill" and entry.total_cents < 0:
        raise Skip("a negative bill is a vendor credit, which is not loaded")
    expense_side = "credit" if refund else "debit"
    expense = [line for line in entry.lines if line.side == expense_side]
    balancing = [line for line in entry.lines if line.side != expense_side]
    if len(balancing) > 1:
        raise Skip(f"more than one {balancing[0].side} line; QuickBooks takes one header account")
    if _line_sum(expense) != abs(entry.total_cents):
        raise Skip("lines do not add up to total_cents; QuickBooks would recompute the total")
    if balancing and balancing[0].amount_cents != abs(entry.total_cents):
        raise Skip("the balancing line does not equal total_cents")

    payload["Line"] = []
    for line in expense:
        accounts.setdefault(line.account, "expense")
        detail = {"AccountRef": {"name": line.account}}
        payload["Line"].append(_line(line, "AccountBasedExpenseLineDetail", detail))

    if entity == "Bill":
        if not entry.vendor_name:
            raise Skip("a bill needs a vendor")
        payload["VendorRef"] = {"name": entry.vendor_name}
        if balancing:
            accounts[balancing[0].account] = "ap"
            payload["APAccountRef"] = {"name": balancing[0].account}
    else:
        if not balancing:
            raise Skip("an expense needs the account it was paid from (its balancing line)")
        paid_with = payment_type(entry, balancing[0].account)
        if refund and paid_with != "CreditCard":
            raise Skip(f"a refund (Credit) is only valid on a CreditCard expense, not {paid_with}")
        given = types.get(balancing[0].account)
        if given and given != PAYMENT_ACCOUNT_TYPES[paid_with]:
            raise Skip(
                f"{balancing[0].account!r} is a {given} account; PaymentType {paid_with} "
                f"needs {PAYMENT_ACCOUNT_TYPES[paid_with]}"
            )
        accounts[balancing[0].account] = paid_with
        payload["PaymentType"] = paid_with
        payload["AccountRef"] = {"name": balancing[0].account}
        if entry.vendor_name:
            payload["EntityRef"] = {"name": entry.vendor_name, "type": "Vendor"}
        if refund:
            payload["Credit"] = True
    return PlannedEntry(entry, entity, payload, accounts, entry.vendor_name, types)


def _line(line: LedgerLine, detail_type: str, detail: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "Amount": to_dollars(line.amount_cents),
        "DetailType": detail_type,
        detail_type: detail,
    }
    if line.description:
        body["Description"] = line.description
    return body


@dataclass
class LoadReport:
    created: list[str] = field(default_factory=list)
    already_loaded: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    new_vendors: list[str] = field(default_factory=list)
    new_accounts: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"created {len(self.created)}",
            f"already loaded {len(self.already_loaded)}",
            f"skipped {len(self.skipped)}",
            f"failed {len(self.failed)}",
            f"new vendors {len(self.new_vendors)}",
            f"new accounts {len(self.new_accounts)}",
        ]
        lines = [", ".join(parts)]
        lines += [f"  skipped {k}: {v}" for k, v in self.skipped.items()]
        lines += [f"  failed {k}: {v}" for k, v in self.failed.items()]
        return "\n".join(lines)


def require_sandbox(config: QboConfig) -> None:
    if config.environment != "sandbox":
        raise QboConfigError(
            "refusing to write: QBO_ENVIRONMENT is not 'sandbox'. "
            "Only the sandbox company ever holds our test data."
        )


def plan_dataset(dataset: Path | str) -> tuple[list[PlannedEntry], dict[str, str]]:
    plans, skipped = [], {}
    for entry in read_jsonl(Path(dataset) / "ledger.jsonl", LedgerEntry):
        try:
            plans.append(plan_entry(entry))
        except Skip as reason:
            skipped[entry.id] = str(reason)
    return plans, skipped


def load_dataset_ledger(dataset: Path | str, client: QboClient) -> LoadReport:
    """Push a dataset's ledger.jsonl into the sandbox. Safe to run again: entries
    already stamped with their dataset id are left alone."""
    require_sandbox(client.config)
    plans, skipped = plan_dataset(dataset)
    report = LoadReport(skipped=skipped)

    loaded = {
        ref
        for entity in ENTITY_TYPES
        for record in client.query(entity)
        if (ref := loaded_ref(record))
    }
    vendors = {v["DisplayName"]: v["Id"] for v in client.query("Vendor")}
    accounts: dict[str, str] = {}
    account_types: dict[str, str] = {}
    for account in client.query("Account"):
        for name in (account["Name"], account.get("FullyQualifiedName", account["Name"])):
            accounts.setdefault(name, account["Id"])
            account_types.setdefault(name, account.get("AccountType", ""))

    def vendor_id(name: str) -> str:
        if name not in vendors:
            vendors[name] = client.create("Vendor", {"DisplayName": name})["Id"]
            report.new_vendors.append(name)
        return vendors[name]

    def account_id(name: str, account_type: str) -> str:
        if name not in accounts:
            body = {"Name": name, "AccountType": account_type}
            accounts[name] = client.create("Account", body)["Id"]
            report.new_accounts.append(name)
        return accounts[name]

    for plan in plans:
        if plan.entry.id in loaded:
            report.already_loaded.append(plan.entry.id)
            continue
        if plan.entity == "Purchase":
            paid_from = plan.payload["AccountRef"]["name"]
            wanted = PAYMENT_ACCOUNT_TYPES[plan.payload["PaymentType"]]
            existing = account_types.get(paid_from)
            if existing is not None and existing != wanted:
                report.skipped[plan.entry.id] = (
                    f"{paid_from!r} is a {existing} account in QuickBooks; "
                    f"PaymentType {plan.payload['PaymentType']} needs {wanted}"
                )
                continue
        # An account QuickBooks already holds with another type than the dataset gives
        # would book the lines under the wrong type; QuickBooks fixes it, not the loader.
        clash = next(
            (
                (name, account_types[name], kind)
                for name, kind in plan.account_types.items()
                if account_types.get(name) not in (None, kind)
            ),
            None,
        )
        if clash:
            report.skipped[plan.entry.id] = (
                f"{clash[0]!r} is a {clash[1]} account in QuickBooks; the dataset says {clash[2]}"
            )
            continue
        try:
            report.created.append(_send(plan, client, vendor_id, account_id))
        except QboError as error:
            report.failed[plan.entry.id] = str(error)
    return report


def _send(
    plan: PlannedEntry,
    client: QboClient,
    vendor_id: Callable[[str], str],
    account_id: Callable[[str, str], str],
) -> str:
    """Resolve names to ids in the planned body and create it. Returns the new ledger id."""

    def resolve(ref: dict[str, Any], account: bool) -> None:
        name = ref.pop("name")
        ref["value"] = account_id(name, plan.account_type(name)) if account else vendor_id(name)

    payload = json.loads(json.dumps(plan.payload))
    for line in payload["Line"]:
        detail = line[line["DetailType"]]
        resolve(detail["AccountRef"], True)
        if "Entity" in detail:
            resolve(detail["Entity"]["EntityRef"], False)
    for key in ("APAccountRef", "AccountRef"):
        if key in payload:
            resolve(payload[key], True)
    for key in ("VendorRef", "EntityRef"):
        if key in payload:
            resolve(payload[key], False)
    created = client.create(plan.entity, payload)
    return f"qbo:{plan.entity}:{created['Id']}"


# --- repairing account types the old loader guessed -------------------------------

# Every entity whose records can point at an account. An account is only repaired
# when every record pointing at it is one the loader stamped; anything else (Intuit's
# sample company, an item, a sub-account) leaves it alone.
ACCOUNT_USERS = (
    *ENTITY_TYPES,
    "Deposit",
    "Transfer",
    "VendorCredit",
    "BillPayment",
    "Payment",
    "SalesReceipt",
    "RefundReceipt",
    "Invoice",
    "CreditMemo",
    "CreditCardPayment",
    "PurchaseOrder",
    "Item",
)
# The old account keeps this name once a correctly typed one has taken its name.
WRONG_TYPE_SUFFIX = " (wrong type, unused)"


def account_subtype(name: str, account_type: str) -> str | None:
    """The AccountSubType to send with a repaired type. None lets QuickBooks pick its
    default; it must not keep the old subtype, which belongs to the old type."""
    if account_type == "Fixed Asset" and "depreciation" in name.lower():
        return "AccumulatedDepreciation"
    return None


def _points_at(obj: Any, account_id: str) -> bool:
    if isinstance(obj, dict):
        return any(
            (
                key.endswith("AccountRef")
                and isinstance(value, dict)
                and value.get("value") == account_id
            )
            or _points_at(value, account_id)
            for key, value in obj.items()
        )
    if isinstance(obj, list):
        return any(_points_at(item, account_id) for item in obj)
    return False


def _repoint(obj: Any, old_id: str, new: dict[str, str]) -> Any:
    """A copy of `obj` with every *AccountRef to `old_id` pointing at `new` instead."""
    if isinstance(obj, dict):
        return {
            key: dict(new)
            if key.endswith("AccountRef")
            and isinstance(value, dict)
            and value.get("value") == old_id
            else _repoint(value, old_id, new)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [_repoint(item, old_id, new) for item in obj]
    return obj


@dataclass
class RepairReport:
    planned: dict[str, str] = field(default_factory=dict)
    fixed: dict[str, str] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    moved: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"planned {len(self.planned)}, fixed {len(self.fixed)}, refused {len(self.refused)}, "
            f"entries moved {len(self.moved)}, failed {len(self.failed)}"
        ]
        for label, items in (
            ("plan", self.planned),
            ("fixed", self.fixed),
            ("refused", self.refused),
            ("failed", self.failed),
        ):
            lines += [f"  {label} {name}: {why}" for name, why in items.items()]
        return "\n".join(lines)


def repair_account_types(
    dataset: Path | str, client: QboClient, write: bool = False
) -> RepairReport:
    """Give every account the dataset uses the AccountType the dataset says, where
    QuickBooks holds it with another one and only our stamped entries use it.

    First an in-place update of the type. If QuickBooks refuses (it often will for an
    account with transactions), the wrong account is renamed with WRONG_TYPE_SUFFIX,
    a correctly typed one is created under the dataset's name, every stamped entry is
    pointed at it (stamp kept), and the old one is made inactive once nothing uses it.
    Dry run unless `write`. Safe to run again: matching accounts are left alone, and an
    account left half moved (still active under the suffixed name) is finished off."""
    require_sandbox(client.config)
    report = RepairReport()
    wanted: dict[str, str] = {}
    for entry in read_jsonl(Path(dataset) / "ledger.jsonl", LedgerEntry):
        for line in entry.lines:
            if line.account_type:
                if wanted.setdefault(line.account, line.account_type) != line.account_type:
                    report.refused[line.account] = "the dataset gives it two account types"
    accounts = client.query("Account")  # active accounts only: inactive ones are done
    by_name: dict[str, dict[str, Any]] = {}
    for account in accounts:
        for name in (account["Name"], account.get("FullyQualifiedName", account["Name"])):
            by_name.setdefault(name, account)
    records = [(entity, r) for entity in ACCOUNT_USERS for r in client.query(entity)]
    records += [("Account", a) for a in accounts if a.get("ParentRef")]  # sub-accounts

    def users(account_id: str) -> tuple[list[tuple[str, dict]], int]:
        """Our stamped records that point at the account, and how many others do."""
        ours, others = [], 0
        for entity, record in records:
            if _points_at(record, account_id):
                if entity in ENTITY_TYPES and loaded_ref(record):
                    ours.append((entity, record))
                else:
                    others += 1
        return ours, others

    def create_account(name: str, kind: str) -> dict[str, Any]:
        body = {"Name": name, "AccountType": kind}
        if subtype := account_subtype(name, kind):
            body["AccountSubType"] = subtype
        return client.create("Account", body)

    def move(old: dict[str, Any], new: dict[str, Any], ours: list[tuple[str, dict]]) -> None:
        """Point our entries at `new`, then make `old` inactive: nothing else used it."""
        target = {"value": new["Id"], "name": new["Name"]}
        for entity, record in ours:
            changed = _repoint(record, old["Id"], target)
            body = {"Id": record["Id"], "SyncToken": record["SyncToken"], "sparse": True}
            body.update({k: v for k, v in changed.items() if v != record.get(k)})
            client.update(entity, body)
            report.moved.append(f"qbo:{entity}:{record['Id']}")
        client.update("Account", {**_editable(old), "Active": False, "sparse": True})

    def retype(account: dict[str, Any], name: str, kind: str, before: str, ours) -> str:
        """Change the type in place; if QuickBooks refuses, replace the account."""
        body = {**_editable(account), "AccountType": kind, "sparse": True}
        # The old subtype belongs to the old type. With none, QuickBooks picks a default.
        if subtype := account_subtype(name, kind):
            body["AccountSubType"] = subtype
        try:
            after = client.update("Account", body)
        except QboError as refusal:
            renamed = client.update(
                "Account", {**_editable(account), "Name": name + WRONG_TYPE_SUFFIX, "sparse": True}
            )
            new = create_account(name, kind)
            move(renamed, new, ours)
            return (
                f"{before} -> {new.get('AccountType')}/{new.get('AccountSubType')} as new "
                f"account {new['Id']}, {len(ours)} entries moved (in place refused: {refusal})"
            )
        return f"{before} -> {after.get('AccountType')}/{after.get('AccountSubType')} in place"

    for name, kind in wanted.items():
        if name in report.refused:
            continue
        account = by_name.get(name)
        leftover = by_name.get(name + WRONG_TYPE_SUFFIX)
        try:
            if leftover and (account is None or account.get("AccountType") == kind):
                # A run stopped part way: finish moving off the renamed account.
                ours, others = users(leftover["Id"])
                if others:
                    report.refused[name] = (
                        f"{leftover['Name']!r} is used by {others} records we did not stamp"
                    )
                elif not write:
                    report.planned[name] = f"finish moving {len(ours)} entries to a {kind}"
                else:
                    move(leftover, account or create_account(name, kind), ours)
                    report.fixed[name] = f"finished: {len(ours)} entries moved to a {kind}"
                continue
            if account is None or account.get("AccountType") == kind:
                continue
            before = f"{account.get('AccountType')}/{account.get('AccountSubType')}"
            ours, others = users(account["Id"])
            if others:
                report.refused[name] = (
                    f"is {before} but {others} records we did not stamp use it; "
                    "only accounts holding nothing but our entries are changed"
                )
            elif not write:
                report.planned[name] = (
                    f"{before} -> {kind}: update in place, or replace it if QuickBooks "
                    f"refuses ({len(ours)} stamped entries use it)"
                )
            else:
                report.fixed[name] = retype(account, name, kind, before, ours)
        except QboError as error:
            report.failed[name] = str(error)
    client._account_types = None  # types read before the repair are stale
    return report


def _editable(account: dict[str, Any]) -> dict[str, Any]:
    """What an Account update sends back: its identity and name, not computed fields."""
    return {key: account[key] for key in ("Id", "SyncToken", "Name") if key in account}


# --- the ledger half of Sources ------------------------------------------------


class QboLedger:
    """`ledger_entries()` and `describe()` for QuickBooks, honestly labelled.

    Live when the sandbox answered and, given a fallback dataset, holds at least one
    of its entries; `detail` then says how many are missing. Otherwise the ledger
    of the local dataset, with the reason in `describe().detail`. The fetch happens once, on first use;
    `describe()` triggers it so it never reports a fetch that did not happen.
    """

    def __init__(
        self,
        fallback: Path | str | None = None,
        client: QboClient | None = None,
        env: Mapping[str, str] = os.environ,
    ):
        self.fallback = Path(fallback) if fallback else None
        self._client = client
        self._env = env
        self._entries: list[LedgerEntry] | None = None
        self._use: SourceUse | None = None

    def _load(self) -> None:
        if self._entries is not None:
            return
        try:
            client = self._client or QboClient(QboConfig.from_env(self._env))
            entries = client.fetch_ledger()
        except (QboError, httpx.HTTPError, KeyError, ValueError) as error:
            reason = str(error) or type(error).__name__
            if self.fallback is None:
                raise QboError(
                    f"QuickBooks not reachable and no local fallback: {reason}"
                ) from error
            self._use_local(LocalSources(self.fallback), reason)
            return
        config = client.config
        detail = (
            f"QuickBooks Online {config.environment} ({config.base_url}), "
            f"read {', '.join(ENTITY_TYPES)}: {len(entries)} entries"
        )
        if self.fallback is not None:
            # The sandbox only means something for this dataset if the loader put
            # the dataset's entries there. Say how much of it is missing.
            local = LocalSources(self.fallback)
            dataset_id = local.dataset.manifest.dataset_id
            wanted = {entry.id for entry in local.ledger_entries()}
            stamped = {ref for entry in entries if (ref := loaded_ref(entry.raw or {}))}
            if wanted and not stamped & wanted:
                self._use_local(
                    local,
                    f"the sandbox holds none of the {len(wanted)} ledger entries of dataset "
                    f"{dataset_id} ({len(entries)} entries read, {len(stamped)} stamped by "
                    "the loader); load them with `python -m auditorium_connectors.qbo load`",
                )
                return
            missing = len(wanted - stamped)
            foreign = sum(1 for entry in entries if loaded_ref(entry.raw or {}) not in wanted)
            detail += (
                f"; dataset {dataset_id}: {missing} of {len(wanted)} entries missing, "
                f"{foreign} entries not from the dataset"
            )
        self._entries = entries
        # A sandbox company holds only test data (ours, or Intuit's sample company).
        self._use = SourceUse(
            system=SourceSystem.QBO,
            mode="live",
            detail=detail,
            synthetic=config.environment == "sandbox",
        )

    def _use_local(self, local: LocalSources, reason: str) -> None:
        self._entries = local.ledger_entries()
        # Same wording as LocalFallback: the local detail, then why it is not live.
        self._use = SourceUse(
            system=SourceSystem.QBO,
            mode="local",
            detail=f"dataset {local.dataset.manifest.dataset_id} at {self.fallback}; "
            f"not live: {reason}",
            synthetic=local.dataset.manifest.synthetic,
        )

    def ledger_entries(self) -> list[LedgerEntry]:
        self._load()
        return list(self._entries or [])

    def describe(self) -> SourceUse:
        self._load()
        assert self._use is not None
        return self._use


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auditorium_connectors.qbo")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="read the ledger from QuickBooks")
    fetch.add_argument("--out", type=Path, help="write ledger entries as JSONL here")
    fetch.add_argument("--fallback", type=Path, help="dataset folder to use if QBO is not live")
    load = commands.add_parser("load", help="push a dataset's ledger.jsonl into the sandbox")
    load.add_argument("dataset", type=Path)
    load.add_argument(
        "--dry-run",
        action="store_true",
        help="plan offline: read nothing from QuickBooks, send nothing",
    )
    repair = commands.add_parser(
        "repair-types",
        help="give the dataset's accounts their dataset AccountType in the sandbox",
    )
    repair.add_argument("dataset", type=Path)
    repair.add_argument(
        "--write", action="store_true", help="change the sandbox; without it, only report"
    )
    args = parser.parse_args(argv)

    try:
        if args.command == "repair-types":
            client = QboClient(QboConfig.from_env())
            report = repair_account_types(args.dataset, client, write=args.write)
            print(("" if args.write else "dry run: ") + report.summary())
            return 1 if report.failed else 0
        if args.command == "fetch":
            ledger = QboLedger(fallback=args.fallback)
            entries = ledger.ledger_entries()
            print(ledger.describe().model_dump_json())
            if args.out:
                write_jsonl(args.out, entries)
            print(f"{len(entries)} ledger entries")
            return 0
        plans, skipped = plan_dataset(args.dataset)
        if args.dry_run:
            # Offline: QuickBooks is not read, so this cannot know what is already there.
            print(
                f"plan: {len(plans)} entries, skip {len(skipped)}; QuickBooks not checked "
                "(a real load skips entries already stamped as loaded)"
            )
            for entry_id, reason in skipped.items():
                print(f"  skip {entry_id}: {reason}")
            return 0
        report = load_dataset_ledger(args.dataset, QboClient(QboConfig.from_env()))
        print(report.summary())
        return 1 if report.failed else 0
    except QboError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
