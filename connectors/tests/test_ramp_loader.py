"""Loading the dataset's bills into the Ramp sandbox, and reading them back. No network.

Ramp is an httpx.MockTransport. Request bodies follow Ramp's OpenAPI spec; the
refusal of a bill whose vendor has no owner is the sandbox's answer on 2026-11-10. Read payloads are CAPTURED from demo-api.ramp.com
(`fixtures/ramp_api_capture.json`); the stamped bill is a captured bill edited by hand.
"""

import importlib.util
import json
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs

import auditorium_connectors
import auditorium_contracts as c
import httpx
import pytest
from auditorium_connectors import ConnectedSources, LocalFallback, QboLedger
from auditorium_connectors.ramp_api import ApiPayloads, ApiRampSources
from auditorium_connectors.ramp_loader import (
    READ_SCOPES,
    REPLACED_KEY,
    WRITE_SCOPES,
    LoadedBillsSources,
    PlannedBill,
    RampLoaderClient,
    RampUnavailable,
    RampWriteRefused,
    load_bills,
    loaded_ref,
    main,
    plan_dataset,
    strip_ref,
    vendor_owner,
)

_spec = importlib.util.spec_from_file_location(
    "ramp_helpers", Path(__file__).with_name("test_ramp.py")
)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
build_local = _helpers.build_local
fake_download = _helpers.fake_download
minimal_pdf = _helpers.minimal_pdf

BASE = "https://demo-api.ramp.com"
TOKEN = "token-test-0001"
SECRET = "secret-test-0001"
CAPTURE = json.loads((Path(__file__).parent / "fixtures" / "ramp_api_capture.json").read_text())
SAVE_TIME_BILLS = CAPTURE["bills"]["data"]
OWNER = SAVE_TIME_BILLS[0]["bill_owner"]["id"]
# The business's account holder, as GET /users lists them (a captured user, edited).
BUSINESS_OWNER = {
    **CAPTURE["users"]["data"][0],
    "id": "user-owner",
    "first_name": "David",
    "last_name": "Wallace",
    "role": "BUSINESS_OWNER",
    "status": "USER_ACTIVE",
}
USERS = [*CAPTURE["users"]["data"], BUSINESS_OWNER]
ENTITIES = [
    {"id": "ent-money", "entity_name": "Save Money Inc", "is_primary": False, "currency": "USD"},
    {"id": "ent-time", "entity_name": "Save Time Inc", "is_primary": True, "currency": "USD"},
]

RENT = "ramp:bill:rent-jan"
CONSULTING = "ramp:bill:consulting-dec"
UNPAID = "ramp:bill:unpaid-feb"
CARD = "ramp:transaction:card-1"
PAM = "local:person:pam"
HOLLY = "local:person:holly"
RENT_APPROVAL = c.Approval(
    approver_id=HOLLY, decision="approved", decided_at=datetime(2026, 1, 5, 9, 30)
)


def _bill(txn_id, vendor, cents, docs, paid=date(2026, 1, 20), memo="Office rent", **extra):
    return c.SpendTransaction(
        id=txn_id,
        kind="vendor_bill",
        source=c.SourceRef(system="ramp", record_type="bill", record_id=txn_id.split(":")[-1]),
        vendor_name=vendor,
        amount_cents=cents,
        transaction_date=date(2026, 1, 1),
        paid_date=paid,
        due_date=date(2026, 1, 31),
        invoice_number=f"INV-{txn_id[-3:]}",
        memo=memo,
        category="Rent & Lease",
        department="Operations",
        document_ids=docs,
        **extra,
    )


def build_dataset(root: Path) -> c.LocalSources:
    """`build_local` plus three bills (one unpaid), a card expense and invoice PDFs."""
    build_local(root)
    docs = []
    for doc_id, kind in (
        ("ramp:bill_attachment:inv-rent", "invoice"),
        ("ramp:bill_attachment:inv-consult", "invoice"),
        ("ramp:bill_attachment:sow-consult", "other"),
        ("ramp:bill_attachment:inv-unpaid", "invoice"),
    ):
        path = root / "documents" / f"{doc_id.split(':')[-1]}.pdf"
        path.write_bytes(minimal_pdf(doc_id))
        docs.append(
            c.Document(
                id=doc_id,
                type=kind,
                source=c.SourceRef(
                    system="ramp", record_type="bill_attachment", record_id=doc_id.split(":")[-1]
                ),
                origin="external" if kind == "invoice" else "internal",
                path=f"documents/{path.name}",
                sha256=c.sha256_file(path),
            )
        )
    existing = c.read_jsonl(root / "documents.jsonl", c.Document)
    c.write_jsonl(root / "documents.jsonl", existing + docs)
    card = c.SpendTransaction(
        id=CARD,
        kind="card_expense",
        source=c.SourceRef(system="ramp", record_type="transaction", record_id="card-1"),
        vendor_name="Air Canada",
        amount_cents=43141,
        transaction_date=date(2025, 12, 3),
        document_ids=["local:receipt:1"],
    )
    c.write_jsonl(
        root / "transactions.jsonl",
        [
            _bill(
                RENT,
                "Blake Street Properties LLC",
                840_000,
                ["ramp:bill_attachment:inv-rent"],
                spender_id=PAM,
                approvals=[RENT_APPROVAL],
            ),
            _bill(
                CONSULTING,
                "Ridgeline Strategy Group LLC",
                1_200_000,
                ["ramp:bill_attachment:inv-consult", "ramp:bill_attachment:sow-consult"],
                memo="Pricing strategy consulting",
            ),
            _bill(
                UNPAID, "Hartwell & Osei LLP", 652_500, ["ramp:bill_attachment:inv-unpaid"], None
            ),
            card,
        ],
    )
    c.write_jsonl(
        root / "people.jsonl",
        [c.Person(id=PAM, name="Pam Beesly"), c.Person(id=HOLLY, name="Holly Flax")],
    )
    return c.LocalSources(root)


def missing_owner_error(n: int) -> dict:
    """Ramp's DeveloperApiErrorResponse; the error id differs on every request."""
    return {
        "error_v2": {
            "error_code": "BILL_PAY_7001",
            "message": "Bill requires a vendor owner for creation",
            "error_id": f"{n:08x}",
            "additional_info": {},
            "notes": "MissingVendorOwnerForBill",
        }
    }


class FakeRampWriter:
    """Ramp's Developer API for the loader: token, list and create endpoints."""

    def __init__(self, bills=(), vendors=(), users=USERS):
        self.bills = [*SAVE_TIME_BILLS, *bills]
        self.vendors = [{"id": "v-figma", "name": "Figma", "vendor_owner_id": "user-owner"}]
        self.vendors += vendors
        self.users = list(users)
        self.requests: list[httpx.Request] = []
        self.token_scopes: list[list[str]] = []
        self.created = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/developer/v1/token":
            scopes = parse_qs(request.content.decode())["scope"][0].split()
            self.token_scopes.append(scopes)
            return httpx.Response(200, json={"access_token": TOKEN, "scope": " ".join(scopes)})
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        listing = {
            "/developer/v1/bills": self.bills,
            "/developer/v1/vendors": self.vendors,
            "/developer/v1/entities": ENTITIES,
            "/developer/v1/users": self.users,
        }
        if method == "GET":
            return httpx.Response(200, json={"data": listing[path], "page": {"next": None}})
        if path == "/developer/v1/bills":
            # As the sandbox answered on 2026-11-10: no bill for a vendor without an owner.
            vendor = next(
                v for v in self.vendors if v["id"] == json.loads(request.content)["vendor_id"]
            )
            if not vendor.get("vendor_owner_id"):
                return httpx.Response(400, json=missing_owner_error(len(self.requests)))
        self.created += 1
        new_id = f"new-{self.created}"
        if path == "/developer/v1/vendors":
            body = json.loads(request.content)
            self.vendors.append(
                {"id": new_id, "name": body["name"], "vendor_owner_id": body.get("vendor_owner_id")}
            )
        elif path == "/developer/v1/bills":
            self.bills.append({"id": new_id, **json.loads(request.content), "attachments": []})
        return httpx.Response(201, json={"id": new_id})

    def posts(self, path: str | None = None) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if r.method == "POST" and r.url.path != "/developer/v1/token"
            if path is None or r.url.path == path
        ]


def client(fake: FakeRampWriter, write=True, base=BASE) -> RampLoaderClient:
    return RampLoaderClient(
        "client-test", SECRET, base, write=write, transport=httpx.MockTransport(fake)
    )


def plans(tmp_path) -> tuple[list[PlannedBill], dict[str, str]]:
    build_dataset(tmp_path / "dataset")
    return plan_dataset(tmp_path / "dataset")


# --- planning ----------------------------------------------------------------------


def test_plan_takes_only_bills_and_skips_what_ramp_would_need_invented(tmp_path):
    planned, skipped = plans(tmp_path)
    assert [p.txn.id for p in planned] == [RENT, CONSULTING]
    assert list(skipped) == [UNPAID] and "unpaid" in skipped[UNPAID]


def test_plan_body_keeps_invoice_dates_number_amount_and_memo(tmp_path):
    rent = plans(tmp_path)[0][0]
    assert rent.body == {
        "invoice_number": "INV-jan",
        "invoice_currency": "USD",
        "issued_at": "2026-01-01",
        "due_at": "2026-01-31",
        "line_items": [{"amount": "8400.00", "memo": "Office rent"}],
        "memo": f"Office rent\nauditorium-ref: {RENT}",
        "payment_method": "PAID_MANUALLY",
        "payment_details": {"manual_payment_method": "OTHER", "payment_date": "2026-01-20"},
        "use_default_vendor_contact": True,
        "enable_accounting_sync": False,
    }


def test_plan_maps_invoice_to_INVOICE_and_other_files_to_FILE(tmp_path):
    consulting = plans(tmp_path)[0][1]
    assert [(a.document_id, a.attachment_type) for a in consulting.attachments] == [
        ("ramp:bill_attachment:inv-consult", "INVOICE"),
        ("ramp:bill_attachment:sow-consult", "FILE"),
    ]
    assert all(a.path.read_bytes().startswith(b"%PDF-") for a in consulting.attachments)


def test_a_bill_with_two_invoices_is_skipped(tmp_path):
    root = tmp_path / "dataset"
    build_dataset(root)
    txns = c.read_jsonl(root / "transactions.jsonl", c.SpendTransaction)
    txns[0] = txns[0].model_copy(
        update={
            "document_ids": ["ramp:bill_attachment:inv-rent", "ramp:bill_attachment:inv-unpaid"]
        }
    )
    c.write_jsonl(root / "transactions.jsonl", txns)
    _, skipped = plan_dataset(root)
    assert "more than one invoice" in skipped[RENT]


# --- the stamp ---------------------------------------------------------------------


def test_stamp_round_trips_through_a_ramp_bill_and_leaves_the_memo_clean(tmp_path):
    rent = plans(tmp_path)[0][0]
    # A captured bill, edited to hold what the loader sent.
    ramp_bill = {**SAVE_TIME_BILLS[0], "id": "ramp-rent", "memo": rent.body["memo"]}
    assert loaded_ref(ramp_bill) == RENT
    mapped = auditorium_connectors.ramp.map_bill(ramp_bill)
    assert loaded_ref(mapped.raw) == RENT
    assert strip_ref(mapped.memo) == "Office rent"


def test_stamp_is_found_if_ramp_folds_the_line_break():
    assert loaded_ref({"memo": f"Office rent auditorium-ref: {RENT}"}) == RENT
    assert strip_ref(f"auditorium-ref: {RENT}") is None
    assert loaded_ref({"memo": "My bill memo"}) is None and loaded_ref(None) is None


# --- loading -------------------------------------------------------------------------


def test_load_creates_vendors_bills_and_attachments(tmp_path):
    fake = FakeRampWriter()
    report = load_bills(*plans(tmp_path), client(fake))
    assert fake.token_scopes == [[*READ_SCOPES, *WRITE_SCOPES]]
    assert report.new_vendors == ["Blake Street Properties LLC", "Ridgeline Strategy Group LLC"]
    assert sorted(report.created) == [CONSULTING, RENT]
    assert report.attachments == 3 and not report.failed
    assert UNPAID in report.skipped
    vendor = json.loads(fake.posts("/developer/v1/vendors")[0].content)
    assert vendor["name"] == "Blake Street Properties LLC"
    assert vendor["business_vendor_contacts"]["email"].endswith("@example.com")
    bill = json.loads(fake.posts("/developer/v1/bills")[0].content)
    assert bill["entity_id"] == "ent-time"  # the primary entity
    assert bill["vendor_id"] == "new-1"
    assert loaded_ref(bill) == RENT


def test_new_vendors_are_owned_by_the_business_owner(tmp_path):
    fake = FakeRampWriter()
    report = load_bills(*plans(tmp_path), client(fake))
    owners = {json.loads(r.content)["vendor_owner_id"] for r in fake.posts("/developer/v1/vendors")}
    assert owners == {"user-owner"}
    assert "new vendors owned by David Wallace (user-owner" in report.summary()
    assert "never the bills' requester" in report.summary()


def test_vendor_owner_is_the_one_active_business_owner():
    admin = {**BUSINESS_OWNER, "id": "user-admin", "role": "BUSINESS_ADMIN"}
    assert vendor_owner([admin, BUSINESS_OWNER])["id"] == "user-owner"
    with pytest.raises(RampUnavailable, match="0 active BUSINESS_OWNER"):
        vendor_owner([admin, {**BUSINESS_OWNER, "status": "USER_SUSPENDED"}])
    with pytest.raises(RampUnavailable, match="2 active BUSINESS_OWNER"):
        vendor_owner([BUSINESS_OWNER, {**BUSINESS_OWNER, "id": "user-other"}])


def test_an_existing_vendor_without_an_owner_skips_its_bill_instead_of_failing(tmp_path):
    ownerless = {"id": "v-blake", "name": "Blake Street Properties LLC"}
    fake = FakeRampWriter(vendors=[ownerless])
    report = load_bills(*plans(tmp_path), client(fake))
    assert "has no owner" in report.skipped[RENT]
    assert RENT not in report.created and not report.failed
    assert [json.loads(r.content)["vendor_id"] for r in fake.posts("/developer/v1/bills")] == [
        "new-1"  # only Ridgeline's bill, for the vendor the loader created with an owner
    ]


def test_ramp_refusing_bills_without_a_vendor_owner_prints_one_line(tmp_path):
    """The first real load: vendors created without an owner, every bill refused."""

    class OwnerlessVendors(RampLoaderClient):
        def create_vendor(self, name, owner_id):
            slug = name.lower().replace(" ", "-")
            body = {"name": name, "business_vendor_contacts": {"email": f"ap+{slug}@example.com"}}
            return self._post("/developer/v1/vendors", json=body)["id"]

    fake = FakeRampWriter()
    loader = OwnerlessVendors(
        "client-test", SECRET, BASE, write=True, transport=httpx.MockTransport(fake)
    )
    report = load_bills(*plans(tmp_path), loader)
    assert sorted(report.failed) == [CONSULTING, RENT] and not report.created
    failed = [line for line in report.summary().splitlines() if "failed" in line][1:]
    assert failed == [
        f"  failed 2 ({RENT}, {CONSULTING}): RampApiError: POST /developer/v1/bills: HTTP 400 "
        'BILL_PAY_7001 "Bill requires a vendor owner for creation" (MissingVendorOwnerForBill)'
    ]


def test_attachment_upload_is_multipart_with_file_and_type(tmp_path):
    fake = FakeRampWriter()
    load_bills(*plans(tmp_path), client(fake))
    uploads = [r for r in fake.posts() if r.url.path.endswith("/attachments")]
    assert [r.url.path for r in uploads] == [
        "/developer/v1/bills/new-2/attachments",  # vendor new-1, bill new-2, file new-3
        "/developer/v1/bills/new-5/attachments",  # vendor new-4, bill new-5
        "/developer/v1/bills/new-5/attachments",
    ]
    first = uploads[0]
    assert first.headers["content-type"].startswith("multipart/form-data; boundary=")
    body = first.read()
    assert b'name="attachment_type"\r\n\r\nINVOICE' in body
    assert b'name="file"; filename="inv-rent.pdf"' in body
    assert b"Content-Type: application/pdf" in body and b"%PDF-" in body
    assert b'name="attachment_type"\r\n\r\nFILE' in uploads[2].read()


def test_an_existing_vendor_is_matched_by_name_ignoring_case_and_spacing(tmp_path):
    fake = FakeRampWriter(
        vendors=[{"id": "v-blake", "name": "blake  street properties llc", "vendor_owner_id": "u"}]
    )
    report = load_bills(*plans(tmp_path), client(fake))
    assert report.new_vendors == ["Ridgeline Strategy Group LLC"]
    bodies = [json.loads(r.content) for r in fake.posts("/developer/v1/bills")]
    assert bodies[0]["vendor_id"] == "v-blake"


def test_two_vendors_with_the_name_skip_the_bill_rather_than_guess(tmp_path):
    twins = [{"id": f"v-{n}", "name": "Blake Street Properties LLC"} for n in (1, 2)]
    report = load_bills(*plans(tmp_path), client(FakeRampWriter(vendors=twins)))
    assert "2 vendors named" in report.skipped[RENT]
    assert RENT not in report.created


def test_a_second_load_creates_nothing_again(tmp_path):
    fake = FakeRampWriter()
    planned, skipped = plans(tmp_path)
    load_bills(planned, skipped, client(fake))
    # Ramp now lists the attachments the first load uploaded.
    for bill in fake.bills[len(SAVE_TIME_BILLS) :]:
        n_files = 1 if loaded_ref(bill) == CONSULTING else 0
        bill["attachments"] = [{"id": "a1", "attachment_type": "INVOICE"}] + [
            {"id": "a2", "attachment_type": "FILE"}
        ] * n_files
    before = len(fake.posts())
    again = load_bills(planned, skipped, client(fake))
    assert sorted(again.already_loaded) == [CONSULTING, RENT]
    assert not again.created and not again.new_vendors and again.attachments == 0
    assert len(fake.posts()) == before


def test_a_loaded_bill_missing_a_file_gets_only_that_file(tmp_path):
    planned, skipped = plans(tmp_path)
    consulting = {
        "id": "ramp-consult",
        "memo": f"Pricing strategy consulting\nauditorium-ref: {CONSULTING}",
        "attachments": [{"id": "a1", "attachment_type": "INVOICE"}],
    }
    fake = FakeRampWriter(bills=[consulting])
    report = load_bills(planned, skipped, client(fake))
    assert report.already_loaded == [CONSULTING]
    uploads = [r for r in fake.posts() if r.url.path.endswith("/attachments")]
    consult_uploads = [r for r in uploads if "ramp-consult" in r.url.path]
    assert len(consult_uploads) == 1 and b"FILE" in consult_uploads[0].read()


def test_save_time_bills_are_never_touched(tmp_path):
    fake = FakeRampWriter()
    load_bills(*plans(tmp_path), client(fake))
    save_time_ids = {b["id"] for b in SAVE_TIME_BILLS}
    assert not any(i in r.url.path for r in fake.posts() for i in save_time_ids)


# --- guards ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "base", ["https://api.ramp.com", "http://demo-api.ramp.com", "https://example.com"]
)
def test_a_writing_client_refuses_any_host_but_the_sandbox(base):
    with pytest.raises(RampWriteRefused, match="only https://demo-api.ramp.com"):
        client(FakeRampWriter(), write=True, base=base)


def test_a_dry_run_client_asks_only_read_scopes_and_never_posts(tmp_path):
    fake = FakeRampWriter()
    report = load_bills(*plans(tmp_path), client(fake, write=False))
    assert fake.token_scopes == [list(READ_SCOPES)]
    assert not fake.posts()
    assert sorted(report.created) == [CONSULTING, RENT] and report.attachments == 3
    assert "would create 2 vendors, 2 bills, 3 attachments" in report.summary()
    with pytest.raises(RampWriteRefused, match="read-only"):
        client(fake, write=False).create_bill({})


@pytest.fixture
def cli(tmp_path, monkeypatch):
    build_dataset(tmp_path / "dataset")
    fake = FakeRampWriter()
    real = RampLoaderClient

    def with_fake(*args, **kwargs):
        return real(*args, **kwargs, transport=httpx.MockTransport(fake))

    monkeypatch.setattr("auditorium_connectors.ramp_loader.RampLoaderClient", with_fake)
    creds = {"RAMP_CLIENT_ID": "client-test", "RAMP_CLIENT_SECRET": SECRET}

    def run(*flags, **env):
        return main([str(tmp_path / "dataset"), *flags], env={**creds, **env})

    return fake, run


def test_cli_without_write_is_a_dry_run(cli, capsys):
    fake, run = cli
    assert run() == 0
    out = capsys.readouterr().out
    assert not fake.posts()
    assert "read-only" in out and "would create 2 vendors, 2 bills, 3 attachments" in out
    assert "dry run: nothing sent" in out and "bills:write vendors:write" in out
    assert SECRET not in out


def test_cli_writes_only_with_write(cli, capsys):
    fake, run = cli
    assert run("--write") == 0
    assert len(fake.posts("/developer/v1/bills")) == 2


def test_cli_write_refuses_production(cli, capsys):
    fake, run = cli
    assert run("--write", RAMP_API_URL="https://api.ramp.com") == 2
    assert "only https://demo-api.ramp.com" in capsys.readouterr().err
    assert not fake.requests


def test_cli_without_credentials_is_offline_and_says_so(tmp_path, capsys):
    build_dataset(tmp_path / "dataset")
    assert main([str(tmp_path / "dataset")], env={}) == 0
    out = capsys.readouterr().out
    assert "plan: 2 bills from 2 vendors with 3 attachments, skip 1" in out
    assert "Ramp not checked" in out
    assert main([str(tmp_path / "dataset"), "--write"], env={}) == 2
    assert "bills:write vendors:write" in capsys.readouterr().err


# --- reading the loaded bills back -------------------------------------------------


def stamped(bill_id: str, ref: str, memo: str, vendor: str, cents: int) -> dict:
    """A captured Save Time bill, edited to be one the loader created."""
    return {
        **SAVE_TIME_BILLS[0],
        "id": bill_id,
        "memo": f"{memo}\nauditorium-ref: {ref}",
        "vendor": {"name": vendor, "type": "BUSINESS", "id": "v-1"},
        "amount": {"amount": cents, "currency_code": "USD", "minor_unit_conversion_rate": 100},
        "attachments": [
            {"id": f"att-{bill_id}", "attachment_type": "INVOICE", "url": f"{BASE}/f/{bill_id}"}
        ],
    }


def api_sources(tmp_path, local, bills) -> ApiRampSources:
    payloads = ApiPayloads(
        transactions=CAPTURE["transactions"]["data"],
        bills=[*SAVE_TIME_BILLS, *bills],
        receipts=CAPTURE["receipts"]["data"] + CAPTURE["receipts_for_transactions"],
        # The capture's users, plus its bills' owner (a captured user, edited).
        users=[*CAPTURE["users"]["data"], {**CAPTURE["users"]["data"][0], "id": OWNER}],
        departments=CAPTURE["departments"]["data"],
    )
    return ApiRampSources(
        payloads, local, endpoint=BASE, cache_dir=tmp_path / "cache", download=fake_download
    )


def mixed(tmp_path) -> LoadedBillsSources:
    local = build_dataset(tmp_path / "dataset")
    rent = stamped("ramp-rent", RENT, "Office rent", "Blake Street Properties LLC", 840_000)
    ramp = api_sources(tmp_path, local, [rent])
    sources, note = auditorium_connectors._ramp_for_dataset(ramp, local)
    assert isinstance(sources, LoadedBillsSources) and note == ""
    return sources


def test_stamped_bills_are_served_live_and_the_rest_from_the_dataset(tmp_path):
    sources = mixed(tmp_path)
    by_id = {t.id: t for t in sources.transactions()}
    assert sorted(by_id) == sorted(["ramp:bill:ramp-rent", CONSULTING, UNPAID, CARD])
    live = by_id["ramp:bill:ramp-rent"]
    assert live.source.record_id == "ramp-rent"  # Ramp's id, not the dataset's
    assert live.memo == "Office rent"
    assert loaded_ref(live.raw) == RENT  # how evals map it back
    assert live.category is None and live.department is None  # not carried, not invented
    assert live.document_ids == ["ramp:bill_attachment:att-ramp-rent"]


def test_save_time_records_are_left_out(tmp_path):
    sources = mixed(tmp_path)
    save_time = {f"ramp:bill:{b['id']}" for b in SAVE_TIME_BILLS} | {
        f"ramp:transaction:{t['id']}" for t in CAPTURE["transactions"]["data"]
    }
    assert not save_time & {t.id for t in sources.transactions()}
    doc_ids = {d.id for d in sources.documents()}
    assert not any(a["id"] in d for b in SAVE_TIME_BILLS for a in b["attachments"] for d in doc_ids)


def test_documents_follow_where_each_record_is_served_from(tmp_path):
    sources = mixed(tmp_path)
    doc_ids = {d.id for d in sources.documents()}
    assert "ramp:bill_attachment:att-ramp-rent" in doc_ids  # live
    assert "ramp:bill_attachment:inv-rent" not in doc_ids  # replaced by the live bill
    assert {"ramp:bill_attachment:inv-consult", "local:receipt:1", "bank:stmt:2025-12"} <= doc_ids
    assert sources.document_file("ramp:bill_attachment:att-ramp-rent").read_bytes()[:5] == b"%PDF-"
    assert sources.document_file("local:receipt:1").name == "local-receipt.txt"


def test_a_live_bill_takes_requester_approvals_and_paid_date_from_the_dataset(tmp_path):
    sources = mixed(tmp_path)
    live = next(t for t in sources.transactions() if t.id == "ramp:bill:ramp-rent")
    assert live.spender_id == PAM
    assert live.approvals == [RENT_APPROVAL]
    assert live.paid_date == date(2026, 1, 20)
    # Everything else is Ramp's; Ramp's own values for the three stay in raw.
    assert (
        live.amount_cents == 840_000 and live.invoice_number == SAVE_TIME_BILLS[0]["invoice_number"]
    )
    assert (
        live.raw["bill_owner"]["id"] == OWNER
        and live.raw["paid_at"] == SAVE_TIME_BILLS[0]["paid_at"]
    )
    replaced = live.raw[REPLACED_KEY]
    assert replaced["spender_id"] == f"ramp:user:{OWNER}"
    assert replaced["approvals"] == [] and set(replaced) == {"spender_id", "approvals", "paid_date"}


def test_the_bill_owner_is_never_presented_as_a_requester_or_person(tmp_path):
    sources = mixed(tmp_path)
    owner = f"ramp:user:{OWNER}"
    assert owner not in {t.spender_id for t in sources.transactions()}
    assert owner not in {a.approver_id for t in sources.transactions() for a in t.approvals}
    # Only the dataset's people: the live bills name no Ramp user.
    assert sorted(p.id for p in sources.people()) == [HOLLY, PAM]


def test_describe_states_the_split(tmp_path):
    use = next(u for u in mixed(tmp_path).describe() if u.system is c.SourceSystem.RAMP)
    assert use.mode == "live"
    assert use.detail.startswith(
        "Ramp Developer API at demo-api.ramp.com | bills: 1 of 3 from Ramp (stamped), 2 from "
        f"dataset tiny (not found in Ramp: {CONSULTING}, {UNPAID}); card transactions: 1 from "
        "dataset tiny (Ramp cannot create card transactions)"
    )
    assert (
        "requester, approvals, paid date from dataset tiny via the stamp; Ramp cannot hold them"
        in use.detail
    )
    assert "other Ramp records ignored (not stamped from this dataset" in use.detail


def test_without_stamped_bills_ramp_falls_back_to_the_dataset_as_before(tmp_path):
    local = build_dataset(tmp_path / "dataset")
    sources, _ = auditorium_connectors._ramp_for_dataset(api_sources(tmp_path, local, []), local)
    assert isinstance(sources, LocalFallback)
    use = next(u for u in sources.describe() if u.system is c.SourceSystem.RAMP)
    assert use.mode == "local" and "Ramp holds a different company" in use.detail


def test_connect_sources_serves_loaded_bills_and_appends_the_ramp_path(tmp_path, monkeypatch):
    local = build_dataset(tmp_path / "dataset")
    rent = stamped("ramp-rent", RENT, "Office rent", "Blake Street Properties LLC", 840_000)
    for name in ("RAMP_MCP_URL", "RAMP_API_URL", "QBO_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RAMP_CLIENT_ID", "client-test")
    monkeypatch.setenv("RAMP_CLIENT_SECRET", SECRET)
    monkeypatch.setattr(
        auditorium_connectors,
        "connect_ramp_api",
        lambda _local, **kw: api_sources(tmp_path, local, [rent]),
    )
    sources = auditorium_connectors.connect_sources(tmp_path / "dataset", cache_dir=tmp_path)
    assert isinstance(sources, ConnectedSources) and isinstance(sources.ledger, QboLedger)
    use = next(u for u in sources.describe() if u.system is c.SourceSystem.RAMP)
    assert "bills: 1 of 3 from Ramp (stamped)" in use.detail
    assert "Ramp path: Developer API" in use.detail
    assert SECRET not in use.detail
