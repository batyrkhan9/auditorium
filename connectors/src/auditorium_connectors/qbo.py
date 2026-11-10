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

    def fetch_ledger(self) -> list[LedgerEntry]:
        """Every Bill, Purchase and JournalEntry. Other entity types are not read."""
        entries: list[LedgerEntry] = []
        for entity in ENTITY_TYPES:
            entries.extend(map_entity(entity, record) for record in self.query(entity))
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


def _expense_lines(record: dict[str, Any], side: str) -> list[LedgerLine]:
    """Account-based lines. Item-based lines name an item, not an account, so they
    are left out here and stay in `raw`."""
    department = _name(record.get("DepartmentRef"))
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
                side=side,
                amount_cents=abs(to_cents(line.get("Amount", 0))),
                description=line.get("Description"),
                department=department,
            )
        )
    return lines


def _balancing_line(account: str | None, side: str, cents: int, department: str | None):
    if not account:
        return []
    return [LedgerLine(account=account, side=side, amount_cents=abs(cents), department=department)]


def map_entity(entity: str, record: dict[str, Any]) -> LedgerEntry:
    """One QBO Bill, Purchase or JournalEntry as a `LedgerEntry`, original in `raw`.

    Bills and Purchases list only their expense lines; the credit side is the
    header's APAccountRef or AccountRef, which QuickBooks posts the total to. It
    becomes the balancing line when the payload names it.
    """
    if entity not in ENTITY_TYPES:
        raise ValueError(f"not a ledger entity: {entity}")
    total = to_cents(record.get("TotalAmt", 0))
    department = _name(record.get("DepartmentRef"))
    vendor: str | None = None

    if entity == "Bill":
        vendor = _name(record.get("VendorRef"))
        lines = _expense_lines(record, "debit") + _balancing_line(
            _name(record.get("APAccountRef")), "credit", total, department
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
        lines = _expense_lines(record, expense_side) + _balancing_line(
            _name(record.get("AccountRef")), card_side, total, department
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
            if entity_info.get("Type") == "Vendor" and _name(entity_info.get("EntityRef")):
                vendors.add(_name(entity_info.get("EntityRef")))
            lines.append(
                LedgerLine(
                    account=account,
                    side="debit" if detail.get("PostingType") == "Debit" else "credit",
                    amount_cents=abs(to_cents(line.get("Amount", 0))),
                    description=line.get("Description"),
                    department=_name(detail.get("DepartmentRef")) or department,
                )
            )
        # A journal entry has no header vendor. When its lines name exactly one,
        # that is the vendor it was booked for.
        vendor = vendors.pop() if len(vendors) == 1 else None
        if "TotalAmt" not in record:
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
        total_cents=total,
        lines=lines,
        raw=record,
    )


# --- LedgerEntry -> QBO payload (the loader) -------------------------------------


class Skip(Exception):
    """This ledger entry cannot be loaded as it is. The message says why."""


# QuickBooks needs an account type to create an account and the contract carries
# only a name, so a new account's type is guessed from its name and its role.
# Only test data in a sandbox is ever loaded, and the type never comes back into
# the contract. The account an expense was paid from has the type its PaymentType
# requires, so QuickBooks accepts the pair.
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
    """QuickBooks' required Purchase.PaymentType. Taken from the entry's raw QBO
    record when it has one, else CreditCard when the paid-from account is a card
    account by name. Cash and Check cannot be told apart from a name, so Skip."""
    recorded = (entry.raw or {}).get("PaymentType")
    if recorded in PAYMENT_ACCOUNT_TYPES:
        return recorded
    if guess_account_type(paid_from, "") == "Credit Card":
        return "CreditCard"
    raise Skip(
        f"cannot tell how the expense was paid: {paid_from!r} is not a card account "
        "by name and the entry records no PaymentType (Cash or Check)"
    )


@dataclass
class PlannedEntry:
    """One ledger entry ready to send: the QBO entity, and its body with names
    still in place of ids. `accounts` maps each account name to its role."""

    entry: LedgerEntry
    entity: str
    payload: dict[str, Any]
    accounts: dict[str, str]
    vendor: str | None


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
            if entry.vendor_name:
                detail["Entity"] = {"Type": "Vendor", "EntityRef": {"name": entry.vendor_name}}
            payload["Line"].append(_line(line, "JournalEntryLineDetail", detail))
        return PlannedEntry(entry, entity, payload, accounts, entry.vendor_name)

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
        accounts[balancing[0].account] = paid_with
        payload["PaymentType"] = paid_with
        payload["AccountRef"] = {"name": balancing[0].account}
        if entry.vendor_name:
            payload["EntityRef"] = {"name": entry.vendor_name, "type": "Vendor"}
        if refund:
            payload["Credit"] = True
    return PlannedEntry(entry, entity, payload, accounts, entry.vendor_name)


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

    def account_id(name: str, role: str) -> str:
        if name not in accounts:
            body = {"Name": name, "AccountType": guess_account_type(name, role)}
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

    def resolve(ref: dict[str, Any], role: str | None) -> None:
        name = ref.pop("name")
        ref["value"] = account_id(name, role) if role else vendor_id(name)

    payload = json.loads(json.dumps(plan.payload))
    for line in payload["Line"]:
        detail = line[line["DetailType"]]
        resolve(detail["AccountRef"], plan.accounts[detail["AccountRef"]["name"]])
        if "Entity" in detail:
            resolve(detail["Entity"]["EntityRef"], None)
    for key in ("APAccountRef", "AccountRef"):
        if key in payload:
            resolve(payload[key], plan.accounts[payload[key]["name"]])
    for key in ("VendorRef", "EntityRef"):
        if key in payload:
            resolve(payload[key], None)
    created = client.create(plan.entity, payload)
    return f"qbo:{plan.entity}:{created['Id']}"


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
        self._use = SourceUse(system=SourceSystem.QBO, mode="live", detail=detail)

    def _use_local(self, local: LocalSources, reason: str) -> None:
        self._entries = local.ledger_entries()
        # Same wording as LocalFallback: the local detail, then why it is not live.
        self._use = SourceUse(
            system=SourceSystem.QBO,
            mode="local",
            detail=f"dataset {local.dataset.manifest.dataset_id} at {self.fallback}; "
            f"not live: {reason}",
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
    args = parser.parse_args(argv)

    try:
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
