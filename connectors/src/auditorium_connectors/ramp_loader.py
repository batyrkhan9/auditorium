"""Load a dataset's vendor bills into the Ramp sandbox, and serve them back live.

Writer: `plan_dataset` turns the dataset's bills into Developer API create bodies;
`load_bills` creates missing vendors, the bills and their invoice files. Each bill's
memo carries an `auditorium-ref: <dataset transaction id>` line, the convention the
QuickBooks loader uses, so a second run skips what the first created and a reader
can map a live bill back to the dataset.

Reader: `LoadedBillsSources` serves the bills Ramp holds with that stamp live, and
the dataset's card transactions from the dataset, because Ramp's API cannot create
card transactions. Ramp's own demo records (Save Time Inc) are left out.

Writing is a dry run unless asked: the CLI writes only with `--write`, and only to
the sandbox host. See connectors/decisions/2026-11-10-ramp-bill-loader.md.

    python -m auditorium_connectors.ramp_loader DATASET [--write]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from auditorium_contracts import (
    Document,
    DocumentType,
    LedgerEntry,
    LocalSources,
    Person,
    Sources,
    SourceSystem,
    SourceUse,
    SpendPolicy,
    SpendTransaction,
    TransactionKind,
)

from .ramp import RampSources, RampUnavailable
from .ramp_api import API_URL, ApiRampSources, RampApiClient, RampApiError

SANDBOX_HOST = "demo-api.ramp.com"
# Same stamp as the QuickBooks loader (qbo.REF_PREFIX).
REF_PREFIX = "auditorium-ref: "
_REF = re.compile(re.escape(REF_PREFIX) + r"(\S+)")
READ_SCOPES = ("bills:read", "vendors:read", "entities:read")
WRITE_SCOPES = ("bills:write", "vendors:write")
BILLS_PATH = "/developer/v1/bills"
VENDORS_PATH = "/developer/v1/vendors"
ENTITIES_PATH = "/developer/v1/entities"
MEMO_MAX = 1000  # the spec's maxLength for a bill memo
# Ramp requires a contact email to create a vendor, and the dataset has none. This
# domain is reserved (RFC 2606) and never delivers. The email never comes back into
# the contract.
CONTACT_DOMAIN = "example.com"
# What a bill in Ramp cannot carry from the dataset. A live bill reads these as Ramp
# has them (empty, or Ramp's own value), never as the dataset had them.
NOT_CARRIED = (
    "category (Ramp codes a bill with accounting-field ids from a connected ERP; the "
    "sandbox has no accounting connection)",
    "department (a bill has no department field)",
    "spender (the create call takes no bill owner)",
    "approvals (only Ramp approvers can make them)",
    "paid date (Ramp sets paid_at itself; the loader sends the dataset's paid date as "
    "payment_date)",
)


class RampWriteRefused(RampUnavailable):
    """A write was asked of a client or host that must not be written to."""


# --- the stamp --------------------------------------------------------------------


def loaded_ref(record: Mapping[str, Any] | None) -> str | None:
    """The dataset transaction id the loader stamped in a Ramp bill's memo, if any.
    Searched anywhere in the memo, so it is found even if Ramp folds the line break."""
    found = _REF.search(str((record or {}).get("memo") or ""))
    return found.group(1) if found else None


def strip_ref(memo: str | None) -> str | None:
    """The memo without the stamp."""
    text = _REF.sub("", memo or "").strip()
    return text or None


# --- planning ---------------------------------------------------------------------


class Skip(Exception):
    """This bill cannot be loaded without inventing something. The message says why."""


@dataclass(frozen=True)
class PlannedAttachment:
    document_id: str
    path: Path
    attachment_type: str  # INVOICE or FILE
    media_type: str


@dataclass
class PlannedBill:
    """One bill ready to send. `body` lacks `vendor_id` and `entity_id` until load."""

    txn: SpendTransaction
    body: dict[str, Any]
    attachments: list[PlannedAttachment]


def _decimal(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def plan_bill(txn: SpendTransaction, local: LocalSources) -> PlannedBill:
    """A Developer API create body for one dataset bill. Raises Skip rather than
    invent a value Ramp requires."""
    if txn.due_date is None:
        raise Skip("no due date; Ramp requires due_at")
    if not txn.invoice_number:
        raise Skip("no invoice number; Ramp requires one")
    if txn.amount_cents <= 0:
        raise Skip("amount is not positive; a credit is a vendor credit in Ramp, not a bill")
    if txn.paid_date is None:
        raise Skip(
            "unpaid in the dataset; Ramp needs a payment method, and every method but "
            "PAID_MANUALLY schedules a real payment that needs vendor bank details we don't have"
        )
    memo = f"{txn.memo}\n{REF_PREFIX}{txn.id}" if txn.memo else f"{REF_PREFIX}{txn.id}"
    if len(memo) > MEMO_MAX:
        raise Skip(f"memo with the stamp is longer than Ramp's {MEMO_MAX} characters")
    documents = {d.id: d for d in local.documents()}
    attachments = []
    for document_id in txn.document_ids:
        document = documents.get(document_id)
        if document is None:
            raise Skip(f"document {document_id} is not in the dataset")
        if document.media_type != "application/pdf" and not document.media_type.startswith(
            "image/"
        ):
            raise Skip(f"document {document_id} is {document.media_type}, not a PDF or image")
        kind = "INVOICE" if document.type is DocumentType.INVOICE else "FILE"
        attachments.append(
            PlannedAttachment(
                document_id, local.document_file(document_id), kind, document.media_type
            )
        )
    if sum(a.attachment_type == "INVOICE" for a in attachments) > 1:
        raise Skip("more than one invoice; Ramp takes one INVOICE attachment per bill")
    body: dict[str, Any] = {
        "invoice_number": txn.invoice_number,
        "invoice_currency": "USD",
        "issued_at": txn.transaction_date.isoformat(),
        "due_at": txn.due_date.isoformat(),
        "line_items": [{"amount": _decimal(txn.amount_cents), "memo": txn.memo}],
        "memo": memo,
        # Recorded as paid outside Ramp: nothing is scheduled or sent to the vendor.
        "payment_method": "PAID_MANUALLY",
        "payment_date": txn.paid_date.isoformat(),
        "use_default_vendor_contact": True,
        # Never pushed on to an accounting system connected to the sandbox.
        "enable_accounting_sync": False,
    }
    if txn.accounting_date:
        body["posting_date"] = txn.accounting_date.isoformat()
    return PlannedBill(txn=txn, body=body, attachments=attachments)


def dataset_bills(local: LocalSources) -> list[SpendTransaction]:
    """The dataset's vendor bills that Ramp holds (source system ramp)."""
    return [
        t
        for t in local.transactions()
        if t.kind is TransactionKind.VENDOR_BILL and t.source.system is SourceSystem.RAMP
    ]


def plan_dataset(dataset: Path | str) -> tuple[list[PlannedBill], dict[str, str]]:
    local = LocalSources(dataset)
    plans, skipped = [], {}
    for txn in dataset_bills(local):
        try:
            plans.append(plan_bill(txn, local))
        except Skip as reason:
            skipped[txn.id] = str(reason)
    return plans, skipped


# --- the client ---------------------------------------------------------------------


def require_sandbox(base_url: str) -> None:
    url = httpx.URL(base_url)
    if url.scheme != "https" or url.host != SANDBOX_HOST:
        raise RampWriteRefused(
            f"refusing to write to {base_url}: only https://{SANDBOX_HOST} (the sandbox) "
            "ever holds our test data"
        )


class RampLoaderClient(RampApiClient):
    """Reads bills, vendors and entities. Writes only when built with `write=True`,
    and only to the sandbox host; a read-only client is never given write scopes."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = API_URL,
        *,
        write: bool = False,
        transport: httpx.BaseTransport | None = None,
    ):
        if write:
            require_sandbox(base_url)
        super().__init__(client_id, client_secret, base_url, transport=transport)
        self.write = write
        self.scopes = READ_SCOPES + (WRITE_SCOPES if write else ())

    def token(self) -> str:
        if self._token:
            return self._token
        response = self._request_token(self.scopes)
        if response.status_code != 200:
            raise RampApiError(
                f"token request for scopes {' '.join(self.scopes)} refused: HTTP "
                f"{response.status_code} {self._scrub(response.text[:200])}; the Ramp app "
                "needs these scopes",
                response.status_code,
            )
        body = response.json()
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise RampUnavailable("token response carries no access_token")
        granted = body.get("scope")
        if isinstance(granted, str):
            missing = [s for s in self.scopes if s not in granted.split()]
            if missing:
                raise RampUnavailable(f"token lacks scopes {missing}; add them to the Ramp app")
        self._token = token
        return token

    def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.write:
            raise RampWriteRefused(f"POST {path} refused: this client is read-only (dry run)")
        require_sandbox(self.base_url)
        response = self._http.post(
            path,
            headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/json"},
            **kwargs,
        )
        if response.status_code not in (200, 201):
            raise RampApiError(
                f"POST {path}: HTTP {response.status_code} {self._scrub(response.text[:300])}",
                response.status_code,
            )
        return response.json()

    def create_vendor(self, name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "vendor"
        body = {"name": name, "business_vendor_contacts": {"email": f"ap+{slug}@{CONTACT_DOMAIN}"}}
        return self._post(VENDORS_PATH, json=body)["id"]

    def create_bill(self, body: Mapping[str, Any]) -> str:
        return self._post(BILLS_PATH, json=dict(body))["id"]

    def upload_attachment(self, bill_id: str, attachment: PlannedAttachment) -> str:
        files = {
            "file": (
                attachment.path.name,
                attachment.path.read_bytes(),
                attachment.media_type,
            )
        }
        return self._post(
            f"{BILLS_PATH}/{bill_id}/attachments",
            data={"attachment_type": attachment.attachment_type},
            files=files,
        )["id"]


# --- loading ----------------------------------------------------------------------


@dataclass
class RampLoadReport:
    write: bool
    checked: str = ""
    created: dict[str, str] = field(default_factory=dict)  # dataset id -> Ramp bill id
    already_loaded: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    new_vendors: list[str] = field(default_factory=list)
    attachments: int = 0

    def summary(self) -> str:
        verb = "" if self.write else "would "
        lines = [
            self.checked,
            f"{verb}create {len(self.new_vendors)} vendors, {len(self.created)} bills, "
            f"{self.attachments} attachments; already loaded {len(self.already_loaded)}, "
            f"skipped {len(self.skipped)}, failed {len(self.failed)}",
        ]
        lines += [f"  skipped {k}: {v}" for k, v in self.skipped.items()]
        lines += [f"  failed {k}: {v}" for k, v in self.failed.items()]
        lines.append("not carried into Ramp: " + "; ".join(NOT_CARRIED))
        return "\n".join(line for line in lines if line)


def _name_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def primary_entity(entities: Iterable[Mapping[str, Any]]) -> Mapping[str, Any]:
    primary = [e for e in entities if e.get("is_primary")]
    if len(primary) != 1 or primary[0].get("currency") not in (None, "USD"):
        raise RampUnavailable("Ramp has no single primary USD business entity to hold the bills")
    return primary[0]


def missing_attachments(plan: PlannedBill, bill: Mapping[str, Any]) -> list[PlannedAttachment]:
    """Planned files a bill in Ramp lacks, by type: if it has n FILE attachments, the
    first n planned FILEs count as there. Ramp lists no file name to match on."""
    held: dict[str, int] = {}
    for attachment in bill.get("attachments") or ():
        kind = str(attachment.get("attachment_type"))
        held[kind] = held.get(kind, 0) + 1
    todo = []
    for attachment in plan.attachments:
        if held.get(attachment.attachment_type, 0) > 0:
            held[attachment.attachment_type] -= 1
        else:
            todo.append(attachment)
    return todo


def load_bills(
    plans: list[PlannedBill], skipped: Mapping[str, str], client: RampLoaderClient
) -> RampLoadReport:
    """Create what Ramp lacks, or with a read-only client say what would be created.
    Safe to run again: a bill already stamped with its dataset id is not created twice,
    only given the files it is missing."""
    write = client.write
    if write:
        require_sandbox(client.base_url)
    report = RampLoadReport(write=write, skipped=dict(skipped))
    existing: dict[str, Mapping[str, Any]] = {}
    bills = client.get_all(BILLS_PATH, {})
    for bill in bills:
        ref = loaded_ref(bill)
        if ref:
            existing.setdefault(ref, bill)
    vendors: dict[str, list[str]] = {}
    for vendor in client.get_all(VENDORS_PATH, {"include_draft": "true"}):
        vendors.setdefault(_name_key(vendor.get("name") or ""), []).append(vendor["id"])
    entity = primary_entity(client.get_all(ENTITIES_PATH, {}))
    wanted = {plan.txn.id for plan in plans}
    report.checked = (
        f"Ramp at {httpx.URL(client.base_url).host} read with scopes {' '.join(client.scopes)}"
        f"{'' if write else ' (read-only)'}: {len(bills)} bills ({len(existing.keys() & wanted)} "
        f"stamped from this dataset), {sum(map(len, vendors.values()))} vendors, entity "
        f"{entity.get('entity_name')}"
    )

    def vendor_id(name: str) -> str:
        found = vendors.get(_name_key(name), [])
        if len(found) > 1:
            raise Skip(f"Ramp has {len(found)} vendors named {name!r}; not guessing which")
        if found:
            return found[0]
        new = client.create_vendor(name) if write else f"(new vendor {name})"
        vendors[_name_key(name)] = [new]
        report.new_vendors.append(name)
        return new

    def upload(bill_id: str, attachments: list[PlannedAttachment]) -> None:
        for attachment in attachments:
            if write:
                client.upload_attachment(bill_id, attachment)
            report.attachments += 1

    for plan in plans:
        ref = plan.txn.id
        try:
            if ref in existing:
                report.already_loaded.append(ref)
                upload(existing[ref]["id"], missing_attachments(plan, existing[ref]))
                continue
            body = {
                **plan.body,
                "vendor_id": vendor_id(plan.txn.vendor_name),
                "entity_id": entity["id"],
            }
            bill_id = client.create_bill(body) if write else "(not created)"
            report.created[ref] = bill_id
            upload(bill_id, plan.attachments)
        except Skip as reason:
            report.skipped[ref] = str(reason)
        except (RampUnavailable, httpx.HTTPError, OSError) as error:
            done = f"bill created as {report.created[ref]}; " if ref in report.created else ""
            report.failed[ref] = f"{done}{type(error).__name__}: {error}"
    return report


# --- reading the loaded bills back --------------------------------------------------


def _ramp_label(ramp: RampSources) -> str:
    host = httpx.URL(ramp.endpoint).host or ramp.endpoint
    if isinstance(ramp, ApiRampSources):
        return f"Ramp Developer API at {host}"
    return f"Ramp MCP {ramp.endpoint}"


def stamped_bills(ramp: Sources, local: LocalSources) -> dict[str, SpendTransaction]:
    """Dataset bill id -> the live Ramp bill stamped with it. The first one wins."""
    wanted = {t.id for t in dataset_bills(local)}
    found: dict[str, SpendTransaction] = {}
    for txn in ramp.transactions():
        if txn.kind is TransactionKind.VENDOR_BILL:
            ref = loaded_ref(txn.raw)
            if ref in wanted and ref not in found:
                found[ref] = txn
    return found


class LoadedBillsSources:
    """The dataset's bills as Ramp holds them, live, where the loader put them there;
    everything else Ramp-side from the dataset. Records Ramp holds that the loader did
    not stamp from this dataset (Save Time's) are left out.

    A live bill keeps its Ramp id; its stamp stays in `raw["memo"]` (see `loaded_ref`)
    and is cut from `memo`. A dataset bill not found in Ramp is served from the dataset.
    Ledger, bank and policy come from the dataset, as with any Ramp path.
    """

    def __init__(self, ramp: RampSources, local: LocalSources):
        self.ramp = ramp
        self.local = local
        live = stamped_bills(ramp, local)
        self.live = {
            ref: txn.model_copy(update={"memo": strip_ref(txn.memo)}) for ref, txn in live.items()
        }
        self.ignored = len(ramp.transactions()) - len(live)
        self._transactions = [self.live.get(t.id, t) for t in local.transactions()]
        # A dataset bill's files go with it when Ramp serves the bill, unless a record
        # still served from the dataset names them too.
        replaced = {d for t in local.transactions() if t.id in live for d in t.document_ids}
        still_named = {d for t in local.transactions() if t.id not in live for d in t.document_ids}
        self._live_docs = {d for t in self.live.values() for d in t.document_ids}
        self._documents = [d for d in ramp.documents() if d.id in self._live_docs] + [
            d for d in local.documents() if d.id not in replaced or d.id in still_named
        ]
        people = {p.id: p for p in local.people()}
        self.local_people = len(people)
        named = {t.spender_id for t in self.live.values()} | {
            a.approver_id for t in self.live.values() for a in t.approvals
        }
        for person in ramp.people():
            if person.id in named:
                people.setdefault(person.id, person)
        self._people = list(people.values())

    def describe(self) -> list[SourceUse]:
        dataset_id = self.local.dataset.manifest.dataset_id
        bills = dataset_bills(self.local)
        missing = [t.id for t in bills if t.id not in self.live]
        cards = [
            t
            for t in self.local.transactions()
            if t.kind is TransactionKind.CARD_EXPENSE and t.source.system is SourceSystem.RAMP
        ]
        split = f"bills: {len(self.live)} of {len(bills)} from Ramp (stamped)"
        if missing:
            named = ", ".join(missing[:5]) + (", ..." if len(missing) > 5 else "")
            split += f", {len(missing)} from dataset {dataset_id} (not found in Ramp: {named})"
        detail = "; ".join(
            [
                f"{_ramp_label(self.ramp)} | {split}",
                f"card transactions: {len(cards)} from dataset {dataset_id} (Ramp cannot "
                "create card transactions)",
                f"people: {self.local_people} from dataset, "
                f"{len(self._people) - self.local_people} from Ramp",
                f"{self.ignored} other Ramp records ignored (not stamped from this dataset, or a "
                "second bill with the same stamp)",
                "Ramp read: " + ", ".join(self.ramp.notes()),
            ]
        )
        mode = "local" if self.ramp.is_standin else "live"
        ramp = SourceUse(system=SourceSystem.RAMP, mode=mode, detail=detail)
        return [ramp] + [u for u in self.local.describe() if u.system is not SourceSystem.RAMP]

    def transactions(self) -> list[SpendTransaction]:
        return list(self._transactions)

    def ledger_entries(self) -> list[LedgerEntry]:
        return self.local.ledger_entries()

    def documents(self) -> list[Document]:
        return list(self._documents)

    def document_file(self, document_id: str) -> Path:
        if document_id in self._live_docs:
            return self.ramp.document_file(document_id)
        return self.local.document_file(document_id)

    def people(self) -> list[Person]:
        return list(self._people)

    def policy(self) -> SpendPolicy:
        return self.local.policy()


# --- CLI ----------------------------------------------------------------------------


def main(argv: list[str] | None = None, env: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(prog="python -m auditorium_connectors.ramp_loader")
    parser.add_argument("dataset", type=Path)
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"create vendors, bills and attachments in Ramp (only https://{SANDBOX_HOST})",
    )
    args = parser.parse_args(argv)
    plans, skipped = plan_dataset(args.dataset)
    client_id = env.get("RAMP_CLIENT_ID", "").strip()
    client_secret = env.get("RAMP_CLIENT_SECRET", "").strip()
    base_url = env.get("RAMP_API_URL", "").strip() or API_URL
    scopes = " ".join(READ_SCOPES + WRITE_SCOPES)
    try:
        if not (client_id and client_secret):
            if args.write:
                raise RampWriteRefused(
                    f"--write needs RAMP_CLIENT_ID and RAMP_CLIENT_SECRET for an app with "
                    f"scopes {scopes}"
                )
            vendors = {_name_key(p.txn.vendor_name) for p in plans}
            print(
                f"plan: {len(plans)} bills from {len(vendors)} vendors with "
                f"{sum(len(p.attachments) for p in plans)} attachments, skip {len(skipped)}; "
                "Ramp not checked (RAMP_CLIENT_ID and RAMP_CLIENT_SECRET not set), so this "
                "cannot know what Ramp already holds"
            )
            for txn_id, reason in skipped.items():
                print(f"  skip {txn_id}: {reason}")
            print("not carried into Ramp: " + "; ".join(NOT_CARRIED))
            print(f"a real load needs --write and an app with scopes {scopes}")
            return 0
        client = RampLoaderClient(client_id, client_secret, base_url, write=args.write)
        report = load_bills(plans, skipped, client)
        print(report.summary())
        if not args.write:
            print(f"dry run: nothing sent; a real load needs --write and scopes {scopes}")
        return 1 if report.failed else 0
    except RampUnavailable as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
