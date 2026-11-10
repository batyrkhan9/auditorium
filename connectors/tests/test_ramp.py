"""Ramp mapping, paging and fallback. No network.

Card transactions, users, org charts, tool names, arguments and error answers come from
`fixtures/ramp_sandbox_capture.json`, captured from Ramp's sandbox MCP server. Bills,
bill invoices and receipts come from `fixtures/ramp_synthetic.json`, which is synthetic
and unconfirmed: the sandbox business has none.

One live test runs only with AUDITORIUM_LIVE_RAMP=1, RAMP_MCP_URL and RAMP_MCP_TOKEN set.
"""

import json
import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import anyio
import auditorium_contracts as c
import httpx
import pytest
from auditorium_connectors import LocalFallback, RampSources, connect_ramp, connect_sources
from auditorium_connectors.ramp import (
    APPROVALS_UNAVAILABLE,
    DEFAULT_TOOLS,
    McpRampClient,
    RampMappingError,
    RampPayloads,
    RampUnavailable,
    ToolCall,
    decimal_usd_cents,
    managers,
    map_bill,
    map_card_transaction,
    map_user,
    read_all,
    records_from,
    refusal_recorder,
    tool_payload,
)

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURE = json.loads((FIXTURES / "ramp_sandbox_capture.json").read_text())
CAPTURED = {tool["name"]: tool for tool in CAPTURE["tools"]}
SYNTHETIC = json.loads((FIXTURES / "ramp_synthetic.json").read_text())


def captured(tool: str):
    return CAPTURED[tool]["response"]


# Real, from Ramp's sandbox.
AIR_CANADA, DINNER, SNACK = captured("ramp_get_transactions")["transactions"]
USERS = captured("ramp_list_users")["users"]
ORG_CHART = captured("ramp_get_org_chart")
DAVID = ORG_CHART["user"]["id"]
# Synthetic and unconfirmed.
BILL = SYNTHETIC["bills"][0]
INVOICES = {bill: answer["attachments"] for bill, answer in SYNTHETIC["bill_invoices"].items()}
RECEIPTS = SYNTHETIC["receipts"]["receipts"]
# A captured transaction with a synthetic receipt attached: the sandbox has no receipts.
WITH_RECEIPT = {**DINNER, "receipt_uuids": [RECEIPTS[0]["id"]]}


def payloads(**changes) -> RampPayloads:
    fields = {
        "transactions": [AIR_CANADA, WITH_RECEIPT, SNACK],
        "bills": [BILL],
        "bill_invoices": INVOICES,
        "receipts": RECEIPTS,
        "users": USERS,
        "org_charts": {DAVID: ORG_CHART},
    }
    return RampPayloads(**{**fields, **changes})


class FixtureClient:
    def __init__(self, result: RampPayloads | Exception):
        self.result = result

    def fetch(self) -> RampPayloads:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def minimal_pdf(text: str) -> bytes:
    """A one-page PDF whose text layer holds `text`, as pypdf reads it."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def fake_download(url: str, dest: Path) -> str:
    dest.write_bytes(minimal_pdf(f"file behind {url}"))
    return "application/pdf"


def html_download(url: str, dest: Path) -> str:
    dest.write_text("<html>Sign in to Ramp</html>")
    return "text/html"


def connect_with(tmp_path: Path, client, download=fake_download):
    return connect_ramp(
        build_local(tmp_path / "dataset"),
        url="https://demo-mcp.ramp.com/mcp",
        token="t",
        cache_dir=tmp_path / "cache",
        client=client,
        download=download,
    )


def build_local(root: Path) -> c.LocalSources:
    (root / "documents").mkdir(parents=True)
    statement = root / "documents" / "statement.txt"
    statement.write_text("Bank statement December 2025\n")
    local_receipt = root / "documents" / "local-receipt.txt"
    local_receipt.write_text("A receipt from the local dataset\n")
    c.write_json(
        root / "manifest.json",
        c.DatasetManifest(
            dataset_id="tiny",
            company_name="Larkspur Labs",
            contract_version=c.CONTRACT_VERSION,
            fiscal_year_start=date(2025, 1, 1),
            fiscal_year_end=date(2025, 12, 31),
            coverage_end=date(2026, 2, 28),
        ),
    )
    c.write_jsonl(root / "transactions.jsonl", [])
    c.write_jsonl(
        root / "ledger.jsonl",
        [
            c.LedgerEntry(
                id="qbo:Bill:147",
                source=c.SourceRef(system="qbo", record_type="Bill", record_id="147"),
                entry_type="bill",
                txn_date=date(2026, 1, 8),
                total_cents=1_200_000,
            )
        ],
    )
    c.write_jsonl(
        root / "documents.jsonl",
        [
            c.Document(
                id="bank:stmt:2025-12",
                type="bank_statement",
                source=c.SourceRef(system="bank", record_type="statement", record_id="2025-12"),
                origin="external",
                path="documents/statement.txt",
                media_type="text/plain",
                sha256=c.sha256_file(statement),
            ),
            c.Document(
                id="local:receipt:1",
                type="receipt",
                source=c.SourceRef(system="ramp", record_type="receipt", record_id="1"),
                origin="external",
                path="documents/local-receipt.txt",
                media_type="text/plain",
                sha256=c.sha256_file(local_receipt),
            ),
        ],
    )
    c.write_jsonl(root / "people.jsonl", [])
    c.write_json(
        root / "policy.json",
        c.SpendPolicy(
            id="policy-2025",
            effective_from=date(2025, 1, 1),
            receipt_required_from_cents=7500,
            approval_rules=[],
        ),
    )
    return c.LocalSources(root)


# --- mapping real payloads ----------------------------------------------------------


def test_captured_card_transaction_maps_to_the_contract():
    txn = map_card_transaction(AIR_CANADA)

    uuid = "2b7a80d1-475d-4dbb-90dc-88f8d9a982ef"
    assert txn.id == f"ramp:transaction:{uuid}"
    assert txn.kind is c.TransactionKind.CARD_EXPENSE
    assert txn.source == c.SourceRef(system="ramp", record_type="transaction", record_id=uuid)
    assert txn.vendor_name == "Air Canada"
    # Settled in USD; the CAD purchase amount stays in raw, unconverted.
    assert txn.amount_cents == 43141
    assert txn.raw["original_amount"] == "608.98" and txn.raw["original_currency"] == "CAD"
    # Dates as Ramp wrote them (UTC), not moved to a local time zone.
    assert txn.transaction_date == date(2026, 11, 29)
    assert txn.accounting_date == date(2026, 11, 30)
    assert txn.paid_date == date(2026, 12, 1)  # a card is paid on its settlement date
    assert txn.memo == "Flight to Toronto"
    # merchant_category ("Airlines") is Ramp's, not the books' category.
    assert txn.category is None
    assert txn.spender_id == "ramp:user:b6b46725-b181-454a-8094-f0e3e2f33e02"
    assert txn.department is None  # Ramp's MCP transaction has no department
    assert txn.document_ids == []
    assert txn.approvals == []  # unknown, not unapproved: see APPROVALS_UNAVAILABLE
    assert txn.raw == AIR_CANADA


def test_amount_agrees_with_ramps_own_cents_for_the_same_transaction():
    metadata = captured("ramp_get_full_transaction_metadata")
    assert metadata["id"] == AIR_CANADA["transaction_uuid"]
    assert map_card_transaction(AIR_CANADA).amount_cents == metadata["amount_cents"]


@pytest.mark.parametrize(
    ("value", "cents"),
    [("431.41", 43141), ("14.05", 1405), ("-180.00", -18000), ("0.10", 10), ("12", 1200)],
)
def test_amount_value_becomes_exact_cents(value, cents):
    assert decimal_usd_cents(value, "USD", "t") == cents


@pytest.mark.parametrize("value", ["1.005", 431.41, 43141, "abc", "NaN", "Infinity", None])
def test_an_amount_that_is_not_exact_cents_is_refused(value):
    with pytest.raises(RampMappingError):
        decimal_usd_cents(value, "USD", "t")


def test_an_amount_not_settled_in_usd_is_refused_not_converted():
    with pytest.raises(RampMappingError, match="settled in EUR"):
        map_card_transaction({**AIR_CANADA, "amount_currency": "EUR"})


def test_receipts_and_gl_account_are_read_when_ramp_sends_them():
    # Shapes unconfirmed: the sandbox sent null for both (no receipts, no accounting provider).
    gl = {"name": "6100 Meals", "category_info": {"type": "GL_ACCOUNT"}}
    txn = map_card_transaction({**WITH_RECEIPT, "accounting_categories": [gl, "odd"]})
    assert txn.document_ids == ["ramp:receipt:r-900"]
    assert txn.category == "6100 Meals"


def test_signed_urls_never_reach_raw():
    signature = "X-Amz-Signature=deadbeefcafe"
    signed = f"https://ramp-files.example/f.pdf?{signature}&X-Amz-Credential=AKIA#frag"
    card = {**AIR_CANADA, "receipt_urls": [signed], "nested": {"deep": [{"url": signed}]}}
    bill = {**BILL, "invoice_urls": [signed], "note": f"see {signed} for the copy"}
    records = [map_card_transaction(card), map_bill(bill)]

    dumped = "".join(r.model_dump_json() for r in records)
    assert "deadbeefcafe" not in dumped
    assert "AKIA" not in dumped
    assert "frag" not in dumped
    # The record stays identifiable: scheme, host and path survive.
    assert records[0].raw["nested"]["deep"][0]["url"] == "https://ramp-files.example/f.pdf"
    assert records[1].raw["note"] == "see https://ramp-files.example/f.pdf for the copy"
    # Ramp's own transaction link has no query and is kept as is.
    assert records[0].raw["transaction_link"] == AIR_CANADA["transaction_link"]


def test_synthetic_bill_maps_invoice_dates_attachments_and_line_item_account():
    txn = map_bill(BILL, INVOICES[BILL["id"]])

    assert txn.id == f"ramp:bill:{BILL['id']}"
    assert txn.kind is c.TransactionKind.VENDOR_BILL
    assert txn.vendor_name == "Northwind Advisory LLC"
    assert txn.amount_cents == 1_200_000
    assert txn.transaction_date == date(2026, 1, 8)
    assert txn.accounting_date == date(2026, 1, 8)
    assert txn.due_date == date(2026, 2, 7)
    assert txn.paid_date == date(2026, 2, 5)
    assert txn.invoice_number == "INV-2041"
    assert txn.category == "6400 Professional Fees"
    assert txn.spender_id == f"ramp:user:{DAVID}"
    assert txn.department is None  # a Ramp bill has no department
    assert txn.document_ids == ["ramp:bill_attachment:att-500"]
    assert txn.approvals == []
    assert txn.raw == BILL


def test_captured_user_maps_without_inventing_a_job_role():
    assert map_user(USERS[0]) == c.Person(
        id="ramp:user:e2e3afa2-b7a2-4a7d-97f1-49ea99e01a12",
        name="Akshit Khurana",
        email="cardholder_23@example.com",
        role=None,  # Ramp's "BUSINESS_USER" is a permission level
        department="Implementations",
        manager_id=None,
    )


def test_managers_come_from_the_captured_org_chart():
    found = managers({DAVID: ORG_CHART})
    assert found == {report["id"]: DAVID for report in ORG_CHART["direct_reports"]}
    michael = ORG_CHART["direct_reports"][0]
    person = map_user({**michael, "role": "BUSINESS_ADMIN"}, found[michael["id"]])
    assert person.name == "Michael Scott"
    assert person.manager_id == f"ramp:user:{DAVID}"


def test_two_managers_for_one_person_is_refused():
    other = {"direct_reports": [ORG_CHART["direct_reports"][0]]}
    with pytest.raises(RampMappingError, match="two managers"):
        managers({DAVID: ORG_CHART, "someone-else": other})


# --- tools, arguments and responses ---------------------------------------------------

# Arguments added per call, on top of a ToolCall's own.
PER_CALL = {"bill_invoices": {"bill_id"}, "receipts": {"receipt_uuids"}, "org_chart": {"user_id"}}


@pytest.mark.parametrize("key", sorted(DEFAULT_TOOLS))
def test_tool_calls_fit_ramps_captured_schemas(key):
    call = DEFAULT_TOOLS[key]
    assert call.name in CAPTURE["tool_names"]
    schema = CAPTURED[call.name]["input_schema"]
    properties = schema["properties"]
    sent = set(call.arguments) | PER_CALL.get(key, set())
    assert set(schema.get("required", [])) <= sent
    assert sent <= set(properties)
    if call.cursor_argument:
        assert call.cursor_argument in properties
    for name, value in call.arguments.items():
        rules = properties[name]
        allowed = rules.get("enum") or (rules.get("items") or {}).get("enum")
        if allowed:
            assert set(value if isinstance(value, list) else [value]) <= set(allowed), name
        if "maximum" in rules:
            assert value <= rules["maximum"], name


@pytest.mark.parametrize("key", ["transactions", "bills", "users"])
def test_records_and_cursor_keys_are_ramps(key):
    call = DEFAULT_TOOLS[key]
    response = captured(call.name)
    assert isinstance(response[call.records_key], list)
    assert call.cursor_field in response


def test_bill_invoice_and_receipt_shapes_are_still_unconfirmed():
    # If these ever hold a response, replace the synthetic fixture with it.
    for key in ("bill_invoices", "receipts"):
        assert captured(DEFAULT_TOOLS[key].name) is None


def structured(payload) -> SimpleNamespace:
    return SimpleNamespace(content=[], is_error=False, structured_content=payload)


def test_tool_payload_reads_structured_or_text_json_and_reports_errors():
    users = DEFAULT_TOOLS["users"]
    text = SimpleNamespace(type="text", text=json.dumps(captured("ramp_list_users")))
    as_text = SimpleNamespace(content=[text], is_error=False, structured_content=None)
    assert records_from(tool_payload(as_text, "T"), users) == USERS

    failed = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="no scope")],
        is_error=True,
        structured_content=None,
    )
    with pytest.raises(RampUnavailable, match="no scope"):
        tool_payload(failed, "T")


@pytest.mark.parametrize(
    ("tool", "message"),
    [
        ("ramp_search_receipts", "Receipt-content search is unavailable"),
        ("ramp_get_tracking_categories", "No accounting provider access"),
        ("ramp_get_approval_chain_details", "not found or is not accessible"),
    ],
)
def test_ramps_error_answers_are_refused(tool, message):
    with pytest.raises(RampUnavailable, match=message):
        tool_payload(structured(captured(tool)), tool)


def test_a_response_without_the_records_key_is_refused():
    with pytest.raises(RampUnavailable, match="no 'users' list"):
        records_from({"data": []}, DEFAULT_TOOLS["users"])


@pytest.mark.parametrize(
    "payload",
    [
        {"bills": [{"id": "x"}], "page": {"next": "https://next"}},
        {"bills": [{"id": "x"}], "has_more": True},
        {"bills": [{"id": "x"}], "next_page": "x"},
        {"bills": [{"id": "x"}], "truncated": True},
    ],
)
def test_a_page_marker_the_tool_does_not_page_with_is_refused(payload):
    with pytest.raises(RampUnavailable, match="pagination not built"):
        records_from(payload, DEFAULT_TOOLS["bills"])


class Pages:
    """A fake tool: answers by cursor, and records the arguments of every call."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def __call__(self, name, arguments):
        self.calls.append(arguments)
        return self.pages[arguments.get("next_page_cursor") or arguments.get("page_cursor")]


def read(pages: Pages, key: str, **extra):
    return anyio.run(lambda: read_all(pages, DEFAULT_TOOLS[key], **extra))


def test_every_page_is_read_by_following_the_cursor():
    first = captured("ramp_get_transactions")
    rest = [{"transaction_uuid": f"t-{n}"} for n in range(1331)]
    pages = Pages(
        {
            None: first,
            "gAAAAA-redacted-cursor": {
                "transactions": rest,
                "total_count": 1334,
                "next_page_cursor": None,
            },
        }
    )
    records = read(pages, "transactions")
    assert len(records) == 1334
    assert pages.calls[1]["next_page_cursor"] == "gAAAAA-redacted-cursor"
    assert all(call["rationale"] for call in pages.calls)


def test_users_page_by_ramps_next_page_user_id():
    first = captured("ramp_list_users")
    pages = Pages({None: first, first["next_page"]: {"users": [], "next_page": None}})
    assert read(pages, "users") == USERS
    assert pages.calls[1]["page_cursor"] == USERS[-1]["id"]


def test_fewer_records_than_counted_is_refused():
    first = captured("ramp_get_transactions")
    pages = Pages({None: first, "gAAAAA-redacted-cursor": {"transactions": []}})
    with pytest.raises(RampUnavailable, match="returned 3 of 1334 records"):
        read(pages, "transactions")


def test_a_repeated_cursor_is_refused_rather_than_looped():
    page = {"bills": [{"id": "x"}], "next_page_cursor": "same"}
    with pytest.raises(RampUnavailable, match="repeated a page cursor"):
        read(Pages({None: page, "same": page}), "bills")


def test_captured_departments_page_counts_more_than_it_holds():
    # The capture cut 13 departments to 3; the same check refuses a real short answer.
    call = ToolCall("ramp_departments", records_key="departments")
    pages = Pages({None: captured("ramp_departments")})
    with pytest.raises(RampUnavailable, match="returned 3 of 13"):
        anyio.run(lambda: read_all(pages, call))


def test_only_auth_refusals_are_recorded():
    refused: list[str] = []
    note = refusal_recorder(refused)
    request = httpx.Request("GET", "https://demo-mcp.ramp.com/mcp")

    async def run():
        await note(httpx.Response(405, text="Method Not Allowed", request=request))
        await note(httpx.Response(500, text="boom", request=request))
        await note(httpx.Response(401, text="invalid_token", request=request))

    anyio.run(run)
    assert refused == ["HTTP 401: invalid_token"]


@pytest.mark.parametrize("grouped", [False, True])
def test_our_own_reason_is_not_replaced_by_an_earlier_refusal(grouped):
    client = McpRampClient("https://demo-mcp.ramp.com/mcp", "t")

    async def fetch(refused):
        refused.append("HTTP 403: forbidden")
        error = RampUnavailable("tool ramp_list_bills returned more than one page")
        raise ExceptionGroup("task group", [error]) if grouped else error

    client._fetch = fetch
    with pytest.raises(RampUnavailable, match="more than one page"):
        client.fetch()


def test_an_sdk_error_after_a_refusal_reports_the_refusal():
    client = McpRampClient("https://demo-mcp.ramp.com/mcp", "t")

    async def fetch(refused):
        refused.append("HTTP 401: invalid_token")
        raise ExceptionGroup("task group", [RuntimeError("Server returned an error response")])

    client._fetch = fetch
    with pytest.raises(RampUnavailable, match="HTTP 401"):
        client.fetch()


# --- sources and fallback ----------------------------------------------------------


def test_ramp_sources_serves_ramp_live_and_the_rest_locally(tmp_path):
    local = build_local(tmp_path / "dataset")
    sources = connect_ramp(
        local,
        url="https://demo-mcp.ramp.com/mcp",
        token=None,
        cache_dir=tmp_path / "cache",
        client=FixtureClient(payloads()),
        download=fake_download,
    )

    assert isinstance(sources, RampSources)
    assert isinstance(sources, c.Sources)
    uses = {use.system: use for use in sources.describe()}
    assert {s: u.mode for s, u in uses.items()} == {
        "ramp": "live",
        "qbo": "local",
        "bank": "local",
        "company": "local",
    }
    assert uses["ramp"].detail == f"Ramp MCP https://demo-mcp.ramp.com/mcp; {APPROVALS_UNAVAILABLE}"

    ids = [t.id for t in sources.transactions()]
    assert len(ids) == 4
    assert f"ramp:bill:{BILL['id']}" in ids

    documents = {d.id: d for d in sources.documents()}
    # Ramp's own files plus the local bank statement; the local copy of a Ramp receipt is not served.
    assert set(documents) == {
        "ramp:receipt:r-900",
        "ramp:bill_attachment:att-500",
        "bank:stmt:2025-12",
    }
    invoice = documents["ramp:bill_attachment:att-500"]
    assert invoice.type is c.DocumentType.INVOICE
    path = sources.document_file(invoice.id)
    assert path.suffix == ".pdf"
    assert c.sha256_file(path) == invoice.sha256
    assert c.quote_on_page(path, 1, "file behind https://example.invalid/presigned/att-500")
    assert sources.document_file("bank:stmt:2025-12").is_file()

    assert [e.id for e in sources.ledger_entries()] == ["qbo:Bill:147"]
    assert sources.policy().id == "policy-2025"
    assert {p.id for p in sources.people()} == {f"ramp:user:{u['id']}" for u in USERS}


def test_pending_and_non_usd_transactions_are_left_out_and_reported(tmp_path):
    pending = {**SNACK, "transaction_uuid": "p-1", "state": "PENDING"}
    euro = {**SNACK, "transaction_uuid": "e-1", "amount_currency": "EUR"}
    declined = {**SNACK, "transaction_uuid": "d-1", "state": "DECLINED"}
    sources = connect_with(
        tmp_path, FixtureClient(payloads(transactions=[AIR_CANADA, pending, euro, declined]))
    )
    assert isinstance(sources, RampSources)
    assert [t.source.record_id for t in sources.transactions()] == [
        AIR_CANADA["transaction_uuid"],
        BILL["id"],
    ]
    detail = {u.system: u for u in sources.describe()}["ramp"].detail
    assert detail.endswith("card transactions left out: 1 not settled in USD, 1 pending")


@pytest.mark.parametrize(
    ("url", "token", "client", "reason"),
    [
        (None, None, None, "RAMP_MCP_URL is not set"),
        ("https://demo-mcp.ramp.com/mcp", None, None, "RAMP_MCP_TOKEN is not set"),
        (
            "https://demo-mcp.ramp.com/mcp",
            "t",
            FixtureClient(RampUnavailable("server lacks tools")),
            "server lacks tools",
        ),
        (
            "https://demo-mcp.ramp.com/mcp",
            "t",
            FixtureClient(payloads(receipts=[])),
            "which Ramp did not return",
        ),
        (
            "https://demo-mcp.ramp.com/mcp",
            "t",
            FixtureClient(payloads(receipts=[{"id": "r-900"}])),
            "receipt r-900: Ramp returned no file URL",
        ),
    ],
)
def test_failure_falls_back_to_local_and_says_why(tmp_path, url, token, client, reason):
    local = build_local(tmp_path / "dataset")
    sources = connect_ramp(
        local,
        url=url,
        token=token,
        cache_dir=tmp_path / "cache",
        client=client,
        download=fake_download,
    )

    assert isinstance(sources, LocalFallback)
    uses = {use.system: use for use in sources.describe()}
    assert all(use.mode == "local" for use in uses.values())
    assert reason in uses["ramp"].detail
    assert "not live" not in uses["qbo"].detail
    assert sources.transactions() == local.transactions()


SIGNED = "https://ramp-files.example/r-900.pdf?X-Amz-Signature=deadbeef&X-Amz-Credential=AKIA"


def test_a_failed_download_names_the_record_and_never_the_signed_url(tmp_path):
    receipt = {**RECEIPTS[0], "receipt_url": SIGNED}

    def refuse(url: str, dest: Path) -> str:
        response = httpx.Response(403, request=httpx.Request("GET", url))
        response.raise_for_status()
        return "application/pdf"

    sources = connect_with(tmp_path, FixtureClient(payloads(receipts=[receipt])), refuse)
    detail = {use.system: use for use in sources.describe()}["ramp"].detail
    assert isinstance(sources, LocalFallback)
    assert "could not download receipt r-900: HTTP 403" in detail
    assert "deadbeef" not in detail and "AKIA" not in detail and "ramp-files" not in detail


def test_urls_in_a_reason_lose_their_query_string(tmp_path):
    error = RampUnavailable(f"tool ramp_get_receipt_details failed: see {SIGNED} for details")
    detail = {u.system: u for u in connect_with(tmp_path, FixtureClient(error)).describe()}
    assert "https://ramp-files.example/r-900.pdf for details" in detail["ramp"].detail
    assert "deadbeef" not in detail["ramp"].detail


@pytest.mark.parametrize(
    "download",
    [
        html_download,
        # A login page that claims to be a PDF.
        lambda url, dest: html_download(url, dest) and "application/pdf",
    ],
)
def test_a_body_that_is_not_a_pdf_or_image_is_refused(tmp_path, download):
    sources = connect_with(tmp_path, FixtureClient(payloads()), download)
    assert isinstance(sources, LocalFallback)
    detail = {u.system: u for u in sources.describe()}["ramp"].detail
    assert "not a PDF or an image" in detail


def test_an_id_that_is_not_plain_never_becomes_a_path(tmp_path):
    receipt = {**RECEIPTS[0], "id": "../../escaped"}
    sources = connect_with(tmp_path, FixtureClient(payloads(receipts=[receipt])))
    assert isinstance(sources, LocalFallback)
    assert "is not a plain id" in {u.system: u for u in sources.describe()}["ramp"].detail
    assert not list(tmp_path.rglob("*escaped*"))


@pytest.mark.parametrize(
    "change", [{"status_summary": "ARCHIVED"}, {"status": "DRAFT", "status_summary": None}]
)
def test_deleted_and_draft_bills_are_not_spend(tmp_path, change):
    sources = connect_with(tmp_path, FixtureClient(payloads(bills=[{**BILL, **change}])))
    assert isinstance(sources, RampSources)
    assert f"ramp:bill:{BILL['id']}" not in [t.id for t in sources.transactions()]


def test_connect_sources_without_ramp_settings_is_local(tmp_path, monkeypatch):
    build_local(tmp_path / "dataset")
    for name in ("RAMP_MCP_URL", "RAMP_MCP_TOKEN", "RAMP_CLIENT_ID", "RAMP_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    sources = connect_sources(tmp_path / "dataset", cache_dir=tmp_path / "cache")
    assert all(use.mode == "local" for use in sources.describe())


def test_connect_sources_without_credentials_is_local_for_every_system(tmp_path, monkeypatch):
    build_local(tmp_path / "dataset")
    for name in (
        "RAMP_MCP_URL",
        "RAMP_MCP_TOKEN",
        "RAMP_CLIENT_ID",
        "RAMP_CLIENT_SECRET",
        *(k for k in os.environ if k.startswith("QBO_")),
    ):
        monkeypatch.delenv(name, raising=False)
    local = c.LocalSources(tmp_path / "dataset")
    sources = connect_sources(tmp_path / "dataset", cache_dir=tmp_path / "cache")
    uses = {use.system: use for use in sources.describe()}
    assert sorted(uses) == ["bank", "company", "qbo", "ramp"]
    assert all(use.mode == "local" for use in uses.values())
    assert "RAMP_MCP_URL" in uses["ramp"].detail
    assert "QBO_CLIENT_ID" in uses["qbo"].detail
    assert sources.ledger_entries() == local.ledger_entries()
    assert sources.transactions() == local.transactions()
    assert sources.documents() == local.documents()
    assert sources.people() == local.people()
    assert sources.policy() == local.policy()


def connect_sources_over(tmp_path, monkeypatch, dataset_transactions):
    """connect_sources with MCP answering the fixture payloads (Air Canada, dinner,
    snack, one bill) and a dataset holding `dataset_transactions`."""
    build_local(tmp_path / "dataset")
    c.write_jsonl(tmp_path / "dataset" / "transactions.jsonl", dataset_transactions)
    for name in (
        "RAMP_CLIENT_ID",
        "RAMP_CLIENT_SECRET",
        *(k for k in os.environ if k.startswith("QBO_")),
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RAMP_MCP_URL", "https://demo-mcp.ramp.com/mcp")
    monkeypatch.setenv("RAMP_MCP_TOKEN", "t")
    monkeypatch.setattr(
        "auditorium_connectors.connect_ramp",
        lambda local, **kw: connect_ramp(
            local, **kw, client=FixtureClient(payloads()), download=fake_download
        ),
    )
    return connect_sources(tmp_path / "dataset", cache_dir=tmp_path / "cache")


def other_company(txn: c.SpendTransaction, ramp_id: str) -> c.SpendTransaction:
    return txn.model_copy(
        update={
            "id": f"ramp:transaction:{ramp_id}",
            "source": c.SourceRef(system="ramp", record_type="transaction", record_id=ramp_id),
        }
    )


def test_ramp_holding_none_of_the_dataset_falls_back_to_the_dataset(tmp_path, monkeypatch):
    mine = [other_company(map_card_transaction(AIR_CANADA), f"copperline-{n}") for n in range(3)]
    sources = connect_sources_over(tmp_path, monkeypatch, mine)
    use = next(u for u in sources.describe() if u.system is c.SourceSystem.RAMP)
    assert use.mode == "local"
    assert (
        "Ramp holds a different company (0 of 3 Ramp transactions of dataset tiny found "
        "among 4 read from Ramp); using the dataset"
    ) in use.detail
    assert "Ramp path: MCP" in use.detail
    assert sources.transactions() == mine


def test_ramp_holding_part_of_the_dataset_stays_live_and_counts_the_gap(tmp_path, monkeypatch):
    shared = map_card_transaction(AIR_CANADA)
    mine = [shared, other_company(shared, "copperline-1")]
    sources = connect_sources_over(tmp_path, monkeypatch, mine)
    use = next(u for u in sources.describe() if u.system is c.SourceSystem.RAMP)
    assert use.mode == "live"
    assert (
        "dataset tiny: 1 of 2 Ramp transactions missing, 3 Ramp transactions not from the dataset"
    ) in use.detail
    assert shared.id in {t.id for t in sources.transactions()}


def test_a_dataset_without_ramp_transactions_does_not_judge_ramp(tmp_path, monkeypatch):
    sources = connect_sources_over(tmp_path, monkeypatch, [])
    use = next(u for u in sources.describe() if u.system is c.SourceSystem.RAMP)
    assert use.mode == "live" and "dataset tiny" not in use.detail


@pytest.mark.skipif(
    os.environ.get("AUDITORIUM_LIVE_RAMP") != "1",
    reason="live Ramp test: set AUDITORIUM_LIVE_RAMP=1, RAMP_MCP_URL and RAMP_MCP_TOKEN",
)
def test_live_ramp_loads(tmp_path):
    sources = connect_ramp(
        build_local(tmp_path / "dataset"),
        url=os.environ["RAMP_MCP_URL"],
        token=os.environ["RAMP_MCP_TOKEN"],
        cache_dir=tmp_path / "cache",
    )
    uses = {use.system: use for use in sources.describe()}
    assert isinstance(sources, RampSources), uses["ramp"].detail
    assert uses["ramp"].mode == "live"
    assert sources.transactions()
