"""Ramp through the Developer API: token, paging, mapping, approvals, fallback. No network.

Every payload here is SPEC-SHAPED, NOT CAPTURED: written by hand from Ramp's OpenAPI
spec (docs.ramp.com/openapi/developer-api.json, read 2026-11-10). Nothing has been
recorded from Ramp's Developer API yet. Approval `step_status` values in particular
are a guess: the spec types them as a free string.

One live test runs only with AUDITORIUM_LIVE_RAMP_API=1, RAMP_CLIENT_ID and
RAMP_CLIENT_SECRET set (RAMP_API_URL optional, default the sandbox).
"""

import base64
import importlib.util
import os
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
SIGNED = "?X-Amz-Signature=abc123&X-Amz-Expires=3600"

# --- spec-shaped payloads ---------------------------------------------------------

DANA, SAM, LEE = "u-dana", "u-sam", "u-lee"
CARD = {
    "id": "t-1",
    "state": "CLEARED",
    "merchant_name": "Figma",
    "merchant_descriptor": "FIGMA.COM",
    "user_transaction_time": "2025-12-03T18:20:00+00:00",
    "accounting_date": "2025-12-04T00:00:00+00:00",
    "settlement_date": "2025-12-05T00:00:00+00:00",
    "entity_amount": {"currency": "USD", "value": 1234},
    "amount": 12.34,
    "minor_unit_conversion_rate": 100,
    "memo": "Design seats",
    "card_holder": {"user_id": DANA, "department_name": "Design", "first_name": "Dana"},
    "accounting_field_selections": [
        {"name": "6100 Software", "category_info": {"type": "GL_ACCOUNT", "name": "GL"}},
        {"name": "Design", "category_info": {"type": "COST_CENTER", "name": "CC"}},
    ],
    "receipts": ["r-1"],
}
REFUND = {
    **CARD,
    "id": "t-2",
    "entity_amount": {"currency": "USD", "value": -500},
    "receipts": [],
    "memo": None,
}
PENDING = {**CARD, "id": "t-3", "state": "PENDING", "receipts": []}
DECLINED = {**CARD, "id": "t-4", "state": "DECLINED", "receipts": []}
IN_CAD = {**CARD, "id": "t-5", "entity_amount": {"currency": "CAD", "value": 900}, "receipts": []}
NO_AMOUNT = {**CARD, "id": "t-6", "entity_amount": None, "receipts": []}
BILL = {
    "id": "b-1",
    "status": "OPEN",
    "status_summary": "AWAITING_PAYMENT",
    "vendor": {"id": "v-1", "name": "Northwind Consulting"},
    "amount": {"amount": 1_200_000, "currency_code": "USD", "minor_unit_conversion_rate": 100},
    "issued_at": "2026-01-08T00:00:00+00:00",
    "accounting_date": "2026-01-08T00:00:00+00:00",
    "due_at": "2026-02-07T00:00:00+00:00",
    "invoice_number": "NW-1042",
    "memo": "December advisory work",
    "bill_owner": {"id": SAM},
    "attachments": [
        {
            "id": "a-1",
            "attachment_type": "INVOICE",
            "url": f"https://files.ramp.com/a-1.pdf{SIGNED}",
        }
    ],
    "invoice_urls": [f"https://files.ramp.com/a-1.pdf{SIGNED}"],
    "accounting_field_selections": [],
    "line_items": [],
}
RECEIPTS = [
    {
        "id": "r-1",
        "transaction_id": "t-1",
        "receipt_url": f"https://files.ramp.com/r-1.jpg{SIGNED}",
    },
    # A reimbursement's receipt: no card transaction names it, so it is not downloaded.
    {"id": "r-9", "reimbursement_id": "re-1", "receipt_url": "https://files.ramp.com/r-9.pdf"},
]
USERS = [
    {
        "id": DANA,
        "first_name": "Dana",
        "last_name": "Reyes",
        "email": "dana@example.com",
        "role": "BUSINESS_USER",
        "department_id": "d-design",
        "manager_id": SAM,
    },
    {
        "id": SAM,
        "first_name": "Sam",
        "last_name": "Okafor",
        "email": "sam@example.com",
        "role": "BUSINESS_ADMIN",
        "department_id": "d-finance",
        "manager_id": None,
    },
]
SUSPENDED = {"id": LEE, "first_name": "Lee", "last_name": "Park", "status": "USER_SUSPENDED"}
DEPARTMENTS = [{"id": "d-design", "name": "Design"}, {"id": "d-finance", "name": "Finance"}]
APPROVALS = [
    {
        "entity_type": "transaction",
        "entity_id": "t-1",
        "status": "APPROVED",
        "next_approvers": [],
        "approval_history": [
            {
                "action_at": "2025-12-06T10:00:00Z",
                "step_status": "APPROVED",
                "user_id": SAM,
                "user_email": "sam@example.com",
            },
        ],
    },
    {
        "entity_type": "bill",
        "entity_id": "b-1",
        "status": "PENDING",
        "next_approvers": [{"user_id": LEE, "user_email": "lee@example.com"}],
        "approval_history": [
            {
                "action_at": "2026-01-09T09:00:00Z",
                "step_status": "REJECTED",
                "user_id": DANA,
                "user_email": None,
            },
            {
                "action_at": "2026-01-10T09:00:00Z",
                "step_status": "APPROVED",
                "user_id": SAM,
                "user_email": None,
            },
            {
                "action_at": "2026-01-10T09:00:01Z",
                "step_status": "APPROVED",
                "user_id": None,
                "user_email": None,
            },
            {
                "action_at": "2026-01-11T09:00:00Z",
                "step_status": "SKIPPED",
                "user_id": LEE,
                "user_email": None,
            },
        ],
    },
]
ALL_SCOPES = " ".join((*REQUIRED_SCOPES, APPROVALS_SCOPE))


class FakeRamp:
    """A Developer API stand-in for httpx.MockTransport. `start` is the page index."""

    def __init__(self, **overrides):
        self.pages = {
            "/developer/v1/transactions": [[CARD, REFUND, PENDING], [DECLINED, IN_CAD, NO_AMOUNT]],
            "/developer/v1/bills": [[BILL]],
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
    assert all(t.approvals == [] for t in sources.transactions())


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
    assert [t["id"] for t in payloads.transactions] == ["t-1", "t-2", "t-3", "t-4", "t-5", "t-6"]
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
    assert {u["id"] for u in payloads.users} == {DANA, SAM, LEE}


# --- mapping -------------------------------------------------------------------------


def test_spec_shaped_card_transaction_maps_to_the_contract():
    txn = map_api_card_transaction(CARD)
    assert txn.id == "ramp:transaction:t-1"
    assert txn.kind is c.TransactionKind.CARD_EXPENSE
    assert txn.vendor_name == "Figma"
    assert txn.amount_cents == 1234
    assert txn.transaction_date == date(2025, 12, 3)
    assert txn.accounting_date == date(2025, 12, 4)
    assert txn.paid_date == date(2025, 12, 5)
    assert txn.memo == "Design seats"
    assert txn.category == "6100 Software"
    assert txn.spender_id == f"ramp:user:{DANA}"
    assert txn.department is None  # the cardholder's department now, not at purchase
    assert txn.document_ids == ["ramp:receipt:r-1"]
    assert txn.raw == CARD


def test_a_refund_is_negative_cents():
    assert map_api_card_transaction(REFUND).amount_cents == -500


def test_a_rate_other_than_cents_is_refused():
    with pytest.raises(RampUnavailable, match="not in cents"):
        map_api_card_transaction({**CARD, "minor_unit_conversion_rate": 1000})


def test_sources_map_bills_people_documents_and_leave_out_what_is_not_spend(tmp_path):
    sources = connect(tmp_path)
    assert isinstance(sources, ApiRampSources)
    by_id = {t.id: t for t in sources.transactions()}
    assert set(by_id) == {"ramp:transaction:t-1", "ramp:transaction:t-2", "ramp:bill:b-1"}
    bill = by_id["ramp:bill:b-1"]
    assert bill.amount_cents == 1_200_000 and bill.invoice_number == "NW-1042"
    assert bill.transaction_date == date(2026, 1, 8)
    assert bill.document_ids == ["ramp:bill_attachment:a-1"]
    assert "Signature" not in str(bill.raw)

    people = {p.id: p for p in sources.people()}
    dana = people[f"ramp:user:{DANA}"]
    assert dana.department == "Design" and dana.manager_id == f"ramp:user:{SAM}"
    assert dana.role is None  # Ramp's role is a permission level, not a job role
    assert f"ramp:user:{LEE}" in people

    ramp_docs = {d.id for d in sources.documents() if d.source.system is c.SourceSystem.RAMP}
    assert ramp_docs >= {"ramp:receipt:r-1", "ramp:bill_attachment:a-1"}
    assert "ramp:receipt:r-9" not in ramp_docs
    assert sources.document_file("ramp:receipt:r-1").exists()

    use = ramp_use(sources)
    assert use.mode == "live"
    assert use.detail.startswith("Ramp Developer API at demo-api.ramp.com")
    assert (
        "card transactions left out: 1 not settled in USD, 1 pending, 1 with no settled amount"
        in use.detail
    )
    assert "Signature" not in use.detail


def test_a_transaction_naming_a_receipt_ramp_did_not_return_fails(tmp_path):
    fake = FakeRamp()
    fake.pages["/developer/v1/receipts"] = [[]]
    sources = connect(tmp_path, fake)
    assert isinstance(sources, LocalFallback)
    assert "ramp:receipt:r-1" in ramp_use(sources).detail


def test_a_file_that_is_not_a_pdf_or_image_fails(tmp_path):
    sources = connect(tmp_path, download=_helpers.html_download)
    assert isinstance(sources, LocalFallback)
    assert "not a PDF or an image" in ramp_use(sources).detail


# --- approvals -----------------------------------------------------------------------


def test_approval_steps_map_to_contract_approvals(tmp_path):
    sources = connect(tmp_path)
    by_id = {t.id: t for t in sources.transactions()}
    assert by_id["ramp:transaction:t-1"].approvals == [
        c.Approval(
            approver_id=f"ramp:user:{SAM}",
            decision="approved",
            decided_at=datetime.fromisoformat("2025-12-06T10:00:00+00:00"),
        )
    ]
    assert [(a.approver_id, a.decision) for a in by_id["ramp:bill:b-1"].approvals] == [
        (f"ramp:user:{DANA}", "rejected"),
        (f"ramp:user:{SAM}", "approved"),
    ]
    # No chain returned for the refund: empty, and describe says what that means.
    assert by_id["ramp:transaction:t-2"].approvals == []
    detail = ramp_use(sources).detail
    assert "approvals from /developer/v1/approvals" in detail
    assert "approval chains not approved: 1 PENDING" in detail
    assert (
        "approval steps left out: 1 approved step without a user, 1 step status SKIPPED" in detail
    )


def test_approvals_are_asked_per_entity_type_by_id(tmp_path):
    fake = FakeRamp()
    connect(tmp_path, fake)
    asked = [r.url.params for r in fake.requests if r.url.path == "/developer/v1/approvals"]
    assert [(p["entity_type"], p.get_list("entity_ids")) for p in asked] == [
        ("TRANSACTION", ["t-1", "t-2", "t-3", "t-5", "t-6"]),  # not the declined t-4
        ("BILL", ["b-1"]),
    ]


@pytest.mark.parametrize("status", [403, 404])
def test_approvals_endpoint_refusal_leaves_approvals_empty_and_says_so(tmp_path, status):
    sources = connect(tmp_path, FakeRamp(approvals_status=status))
    assert isinstance(sources, ApiRampSources)
    assert all(t.approvals == [] for t in sources.transactions())
    use = ramp_use(sources)
    assert use.mode == "live"
    assert (
        f"approvals unavailable (GET /developer/v1/approvals answered HTTP {status})" in use.detail
    )
    assert "unknown, not unapproved" in use.detail


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
