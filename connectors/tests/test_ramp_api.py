"""Ramp through the Developer API: token, paging, mapping, approvals, fallback. No network.

Payloads are CAPTURED from demo-api.ramp.com on 2026-11-10
(`fixtures/ramp_api_capture.json`, sanitized) wherever the capture has them. The
capture has no pending, refunded or non-USD card transaction, no suspended user and
no rejected approval step, so those few records are SPEC-SHAPED: a captured record
edited by hand, or written from Ramp's OpenAPI spec, and labelled where defined.

One live test runs only with AUDITORIUM_LIVE_RAMP_API=1, RAMP_CLIENT_ID and
RAMP_CLIENT_SECRET set (RAMP_API_URL optional, default the sandbox).
"""

import base64
import importlib.util
import json
import os
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs

import auditorium_connectors
import auditorium_contracts as c
import httpx
import pytest
from auditorium_connectors import (
    ApiRampSources,
    LocalFallback,
    RampApiClient,
    connect_ramp_api,
    connect_sources,
)
from auditorium_connectors.ramp import RampUnavailable
from auditorium_connectors.ramp_api import (
    APPROVALS_SCOPE,
    REQUIRED_SCOPES,
    map_api_card_transaction,
    map_approvals,
)

# Helpers shared with test_ramp.py, loaded by path (--import-mode=importlib).
_spec = importlib.util.spec_from_file_location(
    "ramp_helpers", Path(__file__).with_name("test_ramp.py")
)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
build_local = _helpers.build_local
fake_download = _helpers.fake_download

BASE = "https://demo-api.ramp.com"
CLIENT_ID = "client-test-0001"
SECRET = "secret-test-0001"
TOKEN = "token-test-0001"
SIGNED = "?Expires=1762790000&Signature=abc123&Key-Pair-Id=K1"

CAPTURE = json.loads((Path(__file__).parent / "fixtures" / "ramp_api_capture.json").read_text())

# --- captured payloads -------------------------------------------------------------

CAPTURED_CARDS = CAPTURE["transactions"]["data"]
CARD = CAPTURED_CARDS[0]  # Air Canada, $431.41, one receipt, no GL account
# Jamba: the transaction whose receipt file Ramp's file host answered 404 for.
JAMBA = CAPTURE["receipt_transaction"]
JAMBA_RECEIPT = CAPTURE["receipt_single"]
BILLS = CAPTURE["bills"]["data"]  # each has an INVOICE (PDF) and an EMAIL (text) attachment
PAID_BILL = BILLS[0]  # Figma, $549.37, approved by an admin's "Approved" step
ADMIN_APPROVED_BILL = BILLS[1]  # one "Admin approved" step
RECEIPTS = list(
    {
        r["id"]: r for r in CAPTURE["receipts_for_transactions"] + CAPTURE["receipts"]["data"]
    }.values()
)
USERS = CAPTURE["users"]["data"]
DEPARTMENTS = CAPTURE["departments"]["data"]
CAPTURED_APPROVALS = CAPTURE["approvals_transactions"]["data"] + CAPTURE["approvals_bills"]["data"]

# --- spec-shaped payloads (not in the capture) ---------------------------------------

# Captured CARD, edited: a refund, a pending, a declined, a non-USD and an unsettled one.
_NO_RECEIPT = {**CARD, "receipts": []}
REFUND = {**_NO_RECEIPT, "id": "t-refund", "entity_amount": {"currency": "USD", "value": -500}}
PENDING = {**_NO_RECEIPT, "id": "t-pending", "state": "PENDING"}
DECLINED = {**_NO_RECEIPT, "id": "t-declined", "state": "DECLINED"}
IN_CAD = {**_NO_RECEIPT, "id": "t-cad", "entity_amount": {"currency": "CAD", "value": 900}}
NO_AMOUNT = {**_NO_RECEIPT, "id": "t-unsettled", "entity_amount": None}
# From the spec: the capture has no suspended user.
LEE = "u-lee"
SUSPENDED = {"id": LEE, "first_name": "Lee", "last_name": "Park", "status": "USER_SUSPENDED"}
# From the spec, cased like the captured "Approved": a rejection, an approval naming
# no user and a status that is not a decision. None of these was in the capture.
REFUND_CHAIN = {
    "entity_type": "transaction",
    "entity_id": "t-refund",
    "status": "PENDING",
    "next_approvers": [],
    "approval_history": [
        {
            "action_at": "2026-11-01T09:00:00+00:00",
            "step_status": "Rejected",
            "user_id": USERS[0]["id"],
            "user_email": None,
        },
        {
            "action_at": "2026-11-01T09:00:01+00:00",
            "step_status": "Approved",
            "user_id": None,
            "user_email": None,
        },
        {
            "action_at": "2026-11-02T09:00:00+00:00",
            "step_status": "Skipped",
            "user_id": USERS[1]["id"],
            "user_email": None,
        },
    ],
}
APPROVALS = CAPTURED_APPROVALS + [REFUND_CHAIN]
ALL_SCOPES = " ".join((*REQUIRED_SCOPES, APPROVALS_SCOPE))


class FakeRamp:
    """A Developer API stand-in for httpx.MockTransport. `start` is the page index."""

    def __init__(self, **overrides):
        self.pages = {
            "/developer/v1/transactions": [
                [*CAPTURED_CARDS, REFUND],
                [PENDING, DECLINED, IN_CAD, NO_AMOUNT],
            ],
            "/developer/v1/bills": [BILLS],
            "/developer/v1/receipts": [RECEIPTS],
            "/developer/v1/users": [USERS],
            "/developer/v1/departments": [DEPARTMENTS],
        }
        self.token_status = 200
        self.token_body = {
            "access_token": TOKEN,
            "token_type": "Bearer",
            "expires_in": 864000,
            "scope": ALL_SCOPES,
        }
        self.refuse_approvals_scope = False
        self.approvals_status = 200
        self.page_extra = {}
        self.next_host = BASE
        self.requests: list[httpx.Request] = []
        for key, value in overrides.items():
            setattr(self, key, value)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/developer/v1/token":
            scopes = parse_qs(request.content.decode()).get("scope", [""])[0].split()
            if self.refuse_approvals_scope and APPROVALS_SCOPE in scopes:
                return httpx.Response(400, json={"error": "invalid_scope"})
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_client"})
            return httpx.Response(200, json=self.token_body)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        params = request.url.params
        if path == "/developer/v1/approvals":
            if self.approvals_status != 200:
                return httpx.Response(self.approvals_status, json={"error": "forbidden"})
            kind = params["entity_type"].lower()
            ids = params.get_list("entity_ids")
            data = [a for a in APPROVALS if a["entity_type"] == kind and a["entity_id"] in ids]
            return httpx.Response(200, json={"data": data, "page": {"next": None}})
        if path == "/developer/v1/users" and params.get("status") == "USER_SUSPENDED":
            return httpx.Response(200, json={"data": [SUSPENDED], "page": {"next": None}})
        pages = self.pages[path]
        index = int(params.get("start") or 0)
        following = (
            f"{self.next_host}{path}?start={index + 1}&page_size=100"
            if index + 1 < len(pages)
            else None
        )
        return httpx.Response(
            200, json={"data": pages[index], "page": {"next": following, **self.page_extra}}
        )

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def client_for(fake: FakeRamp) -> RampApiClient:
    return RampApiClient(CLIENT_ID, SECRET, BASE, transport=httpx.MockTransport(fake))


def connect(tmp_path: Path, fake: FakeRamp | None = None, download=fake_download):
    return connect_ramp_api(
        build_local(tmp_path / "dataset"),
        client_id=CLIENT_ID,
        client_secret=SECRET,
        base_url=BASE,
        cache_dir=tmp_path / "cache",
        client=client_for(fake or FakeRamp()),
        download=download,
    )


def ramp_use(sources) -> c.SourceUse:
    return next(u for u in sources.describe() if u.system is c.SourceSystem.RAMP)


# --- token ------------------------------------------------------------------------


def test_token_request_is_client_credentials_with_basic_auth_and_scopes():
    fake = FakeRamp()
    assert client_for(fake).token() == TOKEN
    (request,) = fake.requests
    assert request.method == "POST" and request.url == f"{BASE}/developer/v1/token"
    expected = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {expected}"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    body = parse_qs(request.content.decode())
    assert body["grant_type"] == ["client_credentials"]
    assert set(body["scope"][0].split()) == {*REQUIRED_SCOPES, APPROVALS_SCOPE}
    # The secret goes only in the Basic header, never in the body.
    assert SECRET not in request.content.decode()


def test_token_is_fetched_once_per_client():
    fake = FakeRamp()
    client_for(fake).fetch()
    assert fake.paths().count("/developer/v1/token") == 1


def test_an_app_without_approvals_scope_still_connects_and_says_approvals_unavailable(tmp_path):
    fake = FakeRamp(refuse_approvals_scope=True)
    sources = connect(tmp_path, fake)
    assert isinstance(sources, ApiRampSources)
    assert "/developer/v1/approvals" not in fake.paths()
    detail = ramp_use(sources).detail
    assert "approvals unavailable" in detail and APPROVALS_SCOPE in detail
    # Unknown, never an empty list ("never sent for approval").
    assert all(t.approvals is None for t in sources.transactions())


def test_a_token_missing_a_required_scope_is_refused(tmp_path):
    fake = FakeRamp()
    fake.token_body = {**fake.token_body, "scope": "transactions:read approvals:read"}
    sources = connect(tmp_path, fake)
    assert isinstance(sources, LocalFallback)
    assert "bills:read" in ramp_use(sources).detail


# --- secrets -----------------------------------------------------------------------


def test_the_secret_never_reaches_repr_errors_or_describe(tmp_path):
    client = client_for(FakeRamp())
    assert SECRET not in repr(client) and CLIENT_ID not in repr(client)

    def echoing(request: httpx.Request) -> httpx.Response:
        # A hostile or careless server that echoes the credentials and token back.
        return httpx.Response(401, text=f"bad client {CLIENT_ID}:{SECRET} {TOKEN}")

    echo = RampApiClient(CLIENT_ID, SECRET, BASE, transport=httpx.MockTransport(echoing))
    with pytest.raises(RampUnavailable) as raised:
        echo.token()
    assert SECRET not in str(raised.value) and CLIENT_ID not in str(raised.value)
    sources = connect_ramp_api(
        build_local(tmp_path / "dataset"),
        client_id=CLIENT_ID,
        client_secret=SECRET,
        cache_dir=tmp_path / "cache",
        client=echo,
    )
    for use in sources.describe():
        assert SECRET not in use.detail and CLIENT_ID not in use.detail
    assert "HTTP 401" in ramp_use(sources).detail


def test_a_failed_data_call_never_shows_the_token():
    fake = FakeRamp()

    def leaky(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/developer/v1/transactions":
            return httpx.Response(500, text=f"boom {request.headers['authorization']}")
        return fake(request)

    client = RampApiClient(CLIENT_ID, SECRET, BASE, transport=httpx.MockTransport(leaky))
    with pytest.raises(RampUnavailable) as raised:
        client.fetch()
    assert TOKEN not in str(raised.value) and "HTTP 500" in str(raised.value)


# --- paging --------------------------------------------------------------------------


def test_every_page_is_read_by_following_page_next():
    fake = FakeRamp()
    payloads = client_for(fake).fetch()
    assert [t["id"] for t in payloads.transactions] == [
        *(t["id"] for t in CAPTURED_CARDS),
        "t-refund",
        "t-pending",
        "t-declined",
        "t-cad",
        "t-unsettled",
    ]
    pages = [r for r in fake.requests if r.url.path == "/developer/v1/transactions"]
    assert [r.url.params.get("start") for r in pages] == [None, "1"]
    # The first page's filters are kept on the next.
    assert all(r.url.params["state"] == "ALL" for r in pages)
    assert all(r.url.params["page_size"] == "100" for r in pages)


def test_a_next_link_to_another_host_never_receives_the_token():
    fake = FakeRamp(next_host="https://evil.example.com")
    client_for(fake).fetch()
    assert {r.url.host for r in fake.requests} == {"demo-api.ramp.com"}


def test_fewer_records_than_the_stated_total_is_refused():
    fake = FakeRamp(page_extra={"total_count": 99, "total_count_capped": False})
    with pytest.raises(RampUnavailable, match="of 99 records"):
        client_for(fake).fetch()


@pytest.mark.parametrize("marker", [{"has_more": True}, {"cursor": "x"}])
def test_a_more_marker_other_than_next_is_refused(marker):
    fake = FakeRamp(page_extra=marker)
    with pytest.raises(RampUnavailable, match="more than one page"):
        client_for(fake).fetch()


def test_a_repeated_next_is_refused_rather_than_looped():
    fake = FakeRamp()

    def looping(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/developer/v1/transactions":
            return httpx.Response(
                200, json={"data": [CARD], "page": {"next": f"{BASE}{request.url.path}?start=7"}}
            )
        return fake(request)

    client = RampApiClient(CLIENT_ID, SECRET, BASE, transport=httpx.MockTransport(looping))
    with pytest.raises(RampUnavailable, match="repeated a page"):
        client.fetch()


def test_suspended_users_are_read_too():
    payloads = client_for(FakeRamp()).fetch()
    assert {u["id"] for u in payloads.users} == {*(u["id"] for u in USERS), LEE}


# --- mapping -------------------------------------------------------------------------


def test_captured_card_transaction_maps_to_the_contract():
    txn = map_api_card_transaction(CARD)
    assert txn.id == "ramp:transaction:2b7a80d1-475d-4dbb-90dc-88f8d9a982ef"
    assert txn.kind is c.TransactionKind.CARD_EXPENSE
    assert txn.vendor_name == "Air Canada"
    # entity_amount, in USD cents; merchant_amount (CAD) stays in raw.
    assert txn.amount_cents == 43141
    assert txn.transaction_date == date(2026, 11, 29)
    assert txn.accounting_date == date(2026, 11, 30)
    assert txn.paid_date == date(2026, 11, 30)
    assert txn.memo == "Flight to Toronto"
    assert txn.category is None  # no accounting provider in the demo business
    assert txn.spender_id == "ramp:user:b6b46725-b181-454a-8094-f0e3e2f33e02"
    assert txn.department is None  # the cardholder's department now, not at purchase
    assert txn.document_ids == ["ramp:receipt:5afddcd1-98c3-494f-88af-d08a9df37048"]
    assert txn.raw == CARD


def test_a_gl_account_selection_becomes_the_category():
    # Spec-shaped: the demo business has no accounting provider, so none was captured.
    selections = [
        {"name": "6100 Software", "category_info": {"type": "GL_ACCOUNT", "name": "GL"}},
        {"name": "Design", "category_info": {"type": "COST_CENTER", "name": "CC"}},
    ]
    txn = map_api_card_transaction({**CARD, "accounting_field_selections": selections})
    assert txn.category == "6100 Software"


def test_a_refund_is_negative_cents():
    assert map_api_card_transaction(REFUND).amount_cents == -500


def test_a_rate_other_than_cents_is_refused():
    with pytest.raises(RampUnavailable, match="not in cents"):
        map_api_card_transaction({**CARD, "minor_unit_conversion_rate": 1000})


def test_sources_map_bills_people_documents_and_leave_out_what_is_not_spend(tmp_path):
    sources = connect(tmp_path)
    assert isinstance(sources, ApiRampSources)
    by_id = {t.id: t for t in sources.transactions()}
    assert set(by_id) == {
        *(f"ramp:transaction:{t['id']}" for t in CAPTURED_CARDS),
        "ramp:transaction:t-refund",
        *(f"ramp:bill:{b['id']}" for b in BILLS),
    }
    bill = by_id[f"ramp:bill:{PAID_BILL['id']}"]
    assert bill.vendor_name == "Figma"
    assert bill.amount_cents == 54937 and bill.invoice_number == "594896"
    assert bill.transaction_date == date(2026, 11, 10)
    assert bill.paid_date == date(2026, 11, 10) and bill.due_date == date(2026, 12, 10)
    assert bill.document_ids == [
        "ramp:bill_attachment:6c07b311-9a67-4390-bf01-cd7b9fcb8f26",
        "ramp:bill_attachment:aafcea6a-d877-414f-b0f4-76dfac7c5160",
    ]
    assert bill.spender_id == "ramp:user:b6b46725-b181-454a-8094-f0e3e2f33e02"

    people = {p.id: p for p in sources.people()}
    pam = people["ramp:user:8a392daa-72ea-4913-8873-aa5934e82fff"]
    assert pam.name == "Pam Beesly" and pam.department == "Marketing"
    assert pam.manager_id == "ramp:user:37a73512-771d-4807-abe7-c46ff0e72452"
    assert pam.role is None  # Ramp's role is a permission level, not a job role
    assert f"ramp:user:{LEE}" in people

    documents = {d.id: d for d in sources.documents() if d.source.system is c.SourceSystem.RAMP}
    named = {d for t in sources.transactions() for d in t.document_ids}
    assert set(documents) == named
    assert len([d for d in documents if d.startswith("ramp:receipt:")]) == 5
    # A listed receipt no kept transaction names is not downloaded.
    assert f"ramp:receipt:{CAPTURE['receipts']['data'][0]['id']}" not in documents
    assert documents["ramp:bill_attachment:6c07b311-9a67-4390-bf01-cd7b9fcb8f26"].origin == (
        "external"
    )
    assert sources.document_file(next(iter(named))).exists()

    use = ramp_use(sources)
    assert use.mode == "live"
    assert use.detail.startswith(
        "Ramp Developer API at demo-api.ramp.com: 6 card transactions, 5 bills, "
        "15 documents, 6 people; "
    )
    assert (
        "card transactions left out: 1 not settled in USD, 1 pending, 1 with no settled amount"
        in use.detail
    )


def test_signed_file_links_never_reach_raw_or_describe(tmp_path):
    fake = FakeRamp()
    signed = [
        {
            **b,
            "attachments": [{**a, "url": a["url"] + SIGNED} for a in b["attachments"]],
            "invoice_urls": [u + SIGNED for u in b["invoice_urls"]],
        }
        for b in BILLS
    ]
    fake.pages["/developer/v1/bills"] = [signed]
    seen: list[str] = []

    def recording(url: str, dest: Path) -> str:
        seen.append(url)
        return fake_download(url, dest)

    sources = connect(tmp_path, fake, recording)
    assert any("Signature=abc123" in url for url in seen)  # the signed link is used...
    for txn in sources.transactions():  # ...and never kept
        assert "Signature" not in json.dumps(txn.raw)
    assert "Signature" not in ramp_use(sources).detail


def test_a_transaction_naming_a_receipt_ramp_did_not_return_fails(tmp_path):
    fake = FakeRamp()
    fake.pages["/developer/v1/receipts"] = [[]]
    sources = connect(tmp_path, fake)
    assert isinstance(sources, LocalFallback)
    assert f"ramp:receipt:{CARD['receipts'][0]}" in ramp_use(sources).detail


def test_a_file_that_is_not_a_pdf_or_image_fails(tmp_path):
    sources = connect(tmp_path, download=_helpers.html_download)
    assert isinstance(sources, LocalFallback)
    assert "not a PDF or an image" in ramp_use(sources).detail


def answering(status: int, when=lambda url: True):
    """A downloader whose file host answers `status` for the URLs `when` picks."""

    def download(url: str, dest: Path) -> str:
        if when(url):
            response = httpx.Response(status, request=httpx.Request("GET", url))
            response.raise_for_status()
        return fake_download(url, dest)

    return download


def with_jamba() -> FakeRamp:
    """Captured: Jamba's receipt is listed by Ramp, but its file is gone (HTTP 404)."""
    fake = FakeRamp()
    fake.pages["/developer/v1/transactions"][0] = [*CAPTURED_CARDS, REFUND, JAMBA]
    fake.pages["/developer/v1/receipts"] = [[*RECEIPTS, JAMBA_RECEIPT]]
    return fake


def test_a_receipt_whose_file_is_gone_is_left_off_its_transaction_and_named(tmp_path):
    jamba_file = JAMBA_RECEIPT["receipt_url"]
    sources = connect(tmp_path, with_jamba(), answering(404, lambda url: url == jamba_file))
    assert isinstance(sources, ApiRampSources)
    jamba = next(t for t in sources.transactions() if t.source.record_id == JAMBA["id"])
    # Off document_ids, so the receipt check sees no receipt; raw still lists it.
    assert jamba.document_ids == []
    assert jamba.raw["receipts"] == [JAMBA_RECEIPT["id"]]
    receipt = f"ramp:receipt:{JAMBA_RECEIPT['id']}"
    assert receipt not in {d.id for d in sources.documents()}
    use = ramp_use(sources)
    assert use.mode == "live"
    assert (
        "1 document(s) Ramp lists but its file host could not serve, left off their "
        f"transactions so they read as missing: {receipt} (HTTP 404)" in use.detail
    )
    # Every other file is still there.
    assert all(
        t.document_ids for t in sources.transactions() if t.raw.get("receipts") and t is not jamba
    )


def test_many_files_gone_is_a_host_problem_and_fails(tmp_path):
    sources = connect(tmp_path, with_jamba(), answering(404))
    assert isinstance(sources, LocalFallback)
    assert "looks like a host problem" in ramp_use(sources).detail


@pytest.mark.parametrize("status", [403, 500])
def test_a_file_refused_or_failing_still_fails_the_connection(tmp_path, status):
    jamba_file = JAMBA_RECEIPT["receipt_url"]
    sources = connect(tmp_path, with_jamba(), answering(status, lambda url: url == jamba_file))
    assert isinstance(sources, LocalFallback)
    assert f"could not download receipt {JAMBA_RECEIPT['id']}: HTTP {status}" in (
        ramp_use(sources).detail
    )


def text_download(body: str):
    """Captured: Ramp serves a bill's EMAIL attachment as text/plain."""

    def download(url: str, dest: Path) -> str:
        if url.endswith(".txt"):
            dest.write_text(body)
            return "text/plain"
        return fake_download(url, dest)

    return download


def test_a_bills_raw_email_is_kept_as_plain_text(tmp_path):
    sources = connect(tmp_path, download=text_download("Received: from mail\nSubject: Inv\n"))
    assert isinstance(sources, ApiRampSources)
    email = next(
        d for d in sources.documents() if d.id.endswith("aafcea6a-d877-414f-b0f4-76dfac7c5160")
    )
    assert email.media_type == "text/plain" and email.origin == "internal"
    assert sources.document_file(email.id).suffix == ".txt"


def test_html_served_as_a_plain_text_email_is_refused(tmp_path):
    sources = connect(tmp_path, download=text_download("  <html>Sign in to Ramp</html>"))
    assert isinstance(sources, LocalFallback)
    assert "not a PDF or an image" in ramp_use(sources).detail


def test_a_receipt_served_as_plain_text_is_refused(tmp_path):
    def text_everywhere(url: str, dest: Path) -> str:
        dest.write_text("just text")
        return "text/plain"

    sources = connect(tmp_path, download=text_everywhere)
    assert isinstance(sources, LocalFallback)
    assert "receipt" in ramp_use(sources).detail and "not a PDF or an image" in (
        ramp_use(sources).detail
    )


# --- approvals -----------------------------------------------------------------------


def test_approval_steps_map_to_contract_approvals(tmp_path):
    sources = connect(tmp_path)
    by_id = {t.id: t for t in sources.transactions()}
    # Captured: an "Approved" step and an "Admin approved" step, both approvals.
    assert by_id[f"ramp:bill:{PAID_BILL['id']}"].approvals == [
        c.Approval(
            approver_id="ramp:user:783bc20c-08ca-4769-a2f3-6454fd17ee77",
            decision="approved",
            decided_at=datetime.fromisoformat("2026-11-10T08:46:40.233068+00:00"),
        )
    ]
    assert [
        (a.approver_id, a.decision)
        for a in by_id[f"ramp:bill:{ADMIN_APPROVED_BILL['id']}"].approvals
    ] == [("ramp:user:b6b46725-b181-454a-8094-f0e3e2f33e02", "approved")]
    # Captured: a PENDING chain with no history yet is one pending step, waiting on its
    # one next approver, with no decision time.
    assert by_id["ramp:transaction:aa835a3a-0963-468c-a50d-fe747a7a090c"].approvals == [
        c.Approval(approver_id="ramp:user:b6b46725-b181-454a-8094-f0e3e2f33e02", decision="pending")
    ]
    # Spec-shaped: a rejection maps; a step with no user and a non-decision do not. The
    # chain is still PENDING with no next approver: one pending step, nobody named yet.
    assert [(a.approver_id, a.decision) for a in by_id["ramp:transaction:t-refund"].approvals] == [
        (f"ramp:user:{USERS[0]['id']}", "rejected"),
        (None, "pending"),
    ]
    # No chain returned for CARD: empty, and describe says what that means.
    assert by_id[f"ramp:transaction:{CARD['id']}"].approvals == []
    detail = ramp_use(sources).detail
    assert "approvals from /developer/v1/approvals" in detail
    assert "approval chains not approved: 5 PENDING" in detail
    assert "approval steps: 4 Admin approved, 2 Approved, 1 Rejected, 1 Skipped" in detail
    assert (
        "approval steps left out: 1 approved step without a user, 1 step status Skipped" in detail
    )


def test_approvals_are_asked_per_entity_type_by_id(tmp_path):
    fake = FakeRamp()
    connect(tmp_path, fake)
    asked = [r.url.params for r in fake.requests if r.url.path == "/developer/v1/approvals"]
    assert [(p["entity_type"], p.get_list("entity_ids")) for p in asked] == [
        (
            "TRANSACTION",
            [
                *(t["id"] for t in CAPTURED_CARDS),
                "t-refund",
                "t-pending",
                "t-cad",
                "t-unsettled",
            ],  # not the declined one
        ),
        ("BILL", [b["id"] for b in BILLS]),
    ]


@pytest.mark.parametrize("status", [403, 404])
def test_approvals_endpoint_refusal_leaves_approvals_unknown_and_says_so(tmp_path, status):
    sources = connect(tmp_path, FakeRamp(approvals_status=status))
    assert isinstance(sources, ApiRampSources)
    assert all(t.approvals is None for t in sources.transactions())
    use = ramp_use(sources)
    assert use.mode == "live"
    assert (
        f"approvals unavailable (GET /developer/v1/approvals answered HTTP {status})" in use.detail
    )
    assert "every record's approvals are unknown (None)" in use.detail


def test_a_pending_chain_with_several_or_no_next_approvers_has_no_single_approver():
    unmapped = Counter()
    several = {
        "status": "PENDING",
        "approval_history": [
            {
                "step_status": "Approved",
                "user_id": "u-1",
                "action_at": "2026-01-02T10:00:00+00:00",
            }
        ],
        "next_approvers": [{"user_id": "u-2"}, {"user_id": "u-3"}],
    }
    assert [
        (a.approver_id, a.decision, a.decided_at is None) for a in map_approvals(several, unmapped)
    ] == [("ramp:user:u-1", "approved", False), (None, "pending", True)]
    nobody = {"status": "PENDING", "approval_history": [], "next_approvers": []}
    assert map_approvals(nobody, unmapped) == [c.Approval(approver_id=None, decision="pending")]
    # A pending step in the history is not doubled by next_approvers.
    listed = {
        "status": "PENDING",
        "approval_history": [{"step_status": "Pending", "user_id": "u-4", "action_at": None}],
        "next_approvers": [{"user_id": "u-4"}],
    }
    assert map_approvals(listed, unmapped) == [
        c.Approval(approver_id="ramp:user:u-4", decision="pending")
    ]
    assert not unmapped


def test_an_approvals_server_error_fails_the_connection(tmp_path):
    sources = connect(tmp_path, FakeRamp(approvals_status=500))
    assert isinstance(sources, LocalFallback)


# --- fallback and path selection --------------------------------------------------------


def test_without_credentials_it_is_local_and_says_why(tmp_path):
    sources = connect_ramp_api(
        build_local(tmp_path / "dataset"),
        client_id=None,
        client_secret=SECRET,
        cache_dir=tmp_path / "cache",
    )
    assert isinstance(sources, LocalFallback)
    assert "RAMP_CLIENT_ID and RAMP_CLIENT_SECRET" in ramp_use(sources).detail


def test_a_token_refusal_falls_back_to_local(tmp_path):
    sources = connect(tmp_path, FakeRamp(token_status=401))
    assert isinstance(sources, LocalFallback)
    detail = ramp_use(sources).detail
    assert ramp_use(sources).mode == "local"
    assert "Ramp Developer API at demo-api.ramp.com failed" in detail and "HTTP 401" in detail


@pytest.fixture
def clean_env(monkeypatch):
    for name in [k for k in os.environ if k.startswith(("RAMP_", "QBO_"))]:
        monkeypatch.delenv(name)
    return monkeypatch


def recording(monkeypatch, name: str) -> list[dict]:
    calls: list[dict] = []

    def fake(local, **kwargs):
        calls.append(kwargs)
        return LocalFallback(local, {c.SourceSystem.RAMP: f"{name} called"})

    monkeypatch.setattr(auditorium_connectors, name, fake)
    return calls


def test_connect_sources_prefers_the_developer_api(tmp_path, clean_env):
    build_local(tmp_path / "dataset")
    clean_env.setenv("RAMP_CLIENT_ID", CLIENT_ID)
    clean_env.setenv("RAMP_CLIENT_SECRET", SECRET)
    clean_env.setenv("RAMP_MCP_URL", "https://demo-mcp.ramp.com/mcp")
    clean_env.setenv("RAMP_MCP_TOKEN", "t")
    api = recording(clean_env, "connect_ramp_api")
    mcp = recording(clean_env, "connect_ramp")
    sources = connect_sources(tmp_path / "dataset", cache_dir=tmp_path / "cache")
    detail = ramp_use(sources).detail
    assert len(api) == 1 and not mcp
    assert api[0]["base_url"] == "https://demo-api.ramp.com"
    assert "Ramp path: Developer API" in detail
    assert "MCP not tried: the Developer API comes first" in detail
    assert SECRET not in detail


def test_connect_sources_uses_mcp_without_api_credentials(tmp_path, clean_env):
    build_local(tmp_path / "dataset")
    clean_env.setenv("RAMP_CLIENT_ID", CLIENT_ID)  # no secret
    clean_env.setenv("RAMP_MCP_URL", "https://demo-mcp.ramp.com/mcp")
    clean_env.setenv("RAMP_MCP_TOKEN", "t")
    api = recording(clean_env, "connect_ramp_api")
    mcp = recording(clean_env, "connect_ramp")
    sources = connect_sources(tmp_path / "dataset", cache_dir=tmp_path / "cache")
    detail = ramp_use(sources).detail
    assert not api and mcp[0]["url"] == "https://demo-mcp.ramp.com/mcp"
    assert "Ramp path: MCP" in detail
    assert "Developer API not used: RAMP_CLIENT_SECRET not set" in detail


def test_connect_sources_with_nothing_set_is_local_and_names_both_paths(tmp_path, clean_env):
    build_local(tmp_path / "dataset")
    sources = connect_sources(tmp_path / "dataset", cache_dir=tmp_path / "cache")
    use = ramp_use(sources)
    assert use.mode == "local"
    assert "RAMP_MCP_URL is not set" in use.detail
    assert "Developer API not used: RAMP_CLIENT_ID and RAMP_CLIENT_SECRET not set" in use.detail


def test_connect_sources_refuses_a_plain_http_api_url(tmp_path, clean_env):
    build_local(tmp_path / "dataset")
    clean_env.setenv("RAMP_CLIENT_ID", CLIENT_ID)
    clean_env.setenv("RAMP_CLIENT_SECRET", SECRET)
    clean_env.setenv("RAMP_API_URL", "http://demo-api.ramp.com")
    use = ramp_use(connect_sources(tmp_path / "dataset", cache_dir=tmp_path / "cache"))
    assert use.mode == "local" and "must be https" in use.detail


@pytest.mark.skipif(
    os.environ.get("AUDITORIUM_LIVE_RAMP_API") != "1",
    reason="live Ramp Developer API test: set AUDITORIUM_LIVE_RAMP_API=1, RAMP_CLIENT_ID "
    "and RAMP_CLIENT_SECRET",
)
def test_live_ramp_developer_api_loads(tmp_path):
    sources = connect_ramp_api(
        build_local(tmp_path / "dataset"),
        client_id=os.environ["RAMP_CLIENT_ID"],
        client_secret=os.environ["RAMP_CLIENT_SECRET"],
        base_url=os.environ.get("RAMP_API_URL") or "https://demo-api.ramp.com",
        cache_dir=tmp_path / "cache",
    )
    use = ramp_use(sources)
    assert isinstance(sources, ApiRampSources), use.detail
    assert use.mode == "live"
    assert os.environ["RAMP_CLIENT_SECRET"] not in use.detail


def test_the_sandbox_api_is_synthetic(tmp_path):
    use = ramp_use(connect(tmp_path))
    assert use.mode == "live" and use.synthetic is True
