"""Ramp mapping and fallback. No network: the payloads are synthetic fixtures shaped
by Ramp's Developer API spec (see the `_synthetic` note in the fixture file).

One live test runs only with AUDITORIUM_LIVE_RAMP=1, RAMP_MCP_URL and RAMP_MCP_TOKEN set.
"""

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import anyio
import auditorium_contracts as c
import httpx
import pytest
from auditorium_connectors import LocalFallback, RampSources, connect_ramp, connect_sources
from auditorium_connectors.ramp import (
    McpRampClient,
    RampMappingError,
    RampPayloads,
    RampUnavailable,
    approval_chains,
    map_bill,
    map_card_transaction,
    map_user,
    records_from,
    refusal_recorder,
    tool_payload,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "ramp_synthetic.json").read_text())
CARD, DECLINED, REFUND = FIXTURE["transactions"]
BILL = FIXTURE["bills"][0]
APPROVALS = {chain["entity_id"]: chain for chain in FIXTURE["approvals"]}


def payloads(**changes) -> RampPayloads:
    fields = {k: FIXTURE[k] for k in ("transactions", "bills", "receipts", "users", "departments")}
    return RampPayloads(**{**fields, "approvals": APPROVALS, **changes})


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


# --- mapping ---------------------------------------------------------------------


def test_card_transaction_maps_ramp_fields_and_keeps_raw():
    txn = map_card_transaction(CARD, APPROVALS[CARD["id"]])

    assert txn.id == f"ramp:transaction:{CARD['id']}"
    assert txn.kind is c.TransactionKind.CARD_EXPENSE
    assert txn.source == c.SourceRef(system="ramp", record_type="transaction", record_id=CARD["id"])
    assert txn.vendor_name == "Blue Bottle Coffee"
    assert txn.amount_cents == 4250
    assert txn.transaction_date == date(2025, 12, 3)
    assert txn.accounting_date == date(2025, 12, 4)
    assert txn.paid_date is None  # Ramp's settlement_date stays in raw; see the decision file
    assert txn.category == "6100 Meals"  # the GL_ACCOUNT selection, not Ramp's own category
    assert txn.spender_id == "ramp:user:u-200"
    assert txn.department == "Engineering"
    assert txn.document_ids == ["ramp:receipt:r-900"]
    assert txn.approvals == [
        c.Approval(
            approver_id="ramp:user:u-100",
            decision="approved",
            decided_at=datetime(2025, 12, 4, 10, tzinfo=UTC),
        )
    ]
    assert txn.raw == CARD


def test_refund_keeps_its_negative_amount_and_no_invented_fields():
    txn = map_card_transaction(REFUND)
    assert txn.amount_cents == -18000
    assert txn.accounting_date is None
    assert txn.category is None
    assert txn.memo is None
    assert txn.approvals == []


def test_bill_maps_invoice_dates_and_line_item_account():
    txn = map_bill(BILL, APPROVALS[BILL["id"]])

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
    assert txn.spender_id == "ramp:user:u-100"
    assert txn.department is None  # a Ramp bill has no department
    assert txn.document_ids == ["ramp:bill_attachment:att-500"]
    # The PENDING step is not a decision.
    assert [a.decision for a in txn.approvals] == ["approved"]
    assert txn.raw == BILL


def test_user_maps_without_inventing_a_job_role():
    departments = {d["id"]: d["name"] for d in FIXTURE["departments"]}
    dana = map_user(FIXTURE["users"][1], departments)
    assert dana == c.Person(
        id="ramp:user:u-200",
        name="Dana Reyes",
        email="dana@example.com",
        role=None,
        department="Engineering",
        manager_id="ramp:user:u-100",
    )


def test_non_usd_amount_is_refused():
    euro = {**CARD, "entity_amount": {"currency": "EUR", "value": 4250}}
    with pytest.raises(RampMappingError):
        map_card_transaction(euro)


# --- tool responses --------------------------------------------------------------


def test_tool_payload_reads_structured_or_text_json_and_reports_errors():
    text = SimpleNamespace(type="text", text='{"data": [{"id": "x"}]}')
    as_text = SimpleNamespace(content=[text], is_error=False, structured_content=None)
    assert records_from(tool_payload(as_text, "T"), "T") == [{"id": "x"}]

    structured = SimpleNamespace(content=[], is_error=False, structured_content={"result": []})
    assert records_from(tool_payload(structured, "T"), "T") == []

    failed = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="no scope")],
        is_error=True,
        structured_content=None,
    )
    with pytest.raises(RampUnavailable, match="no scope"):
        tool_payload(failed, "T")


def test_a_second_page_is_refused_rather_than_dropped():
    with pytest.raises(RampUnavailable, match="more than one page"):
        records_from({"data": [{"id": "x"}], "page": {"next": "https://next"}}, "T")


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"id": "x"}], "total": 2},
        {"data": [{"id": "x"}], "page": {"total_count": 40}},
        {"data": [{"id": "x"}], "cursor": "abc"},
        {"items": [{"id": "x"}], "truncated": True},
    ],
)
def test_a_partial_response_is_refused(payload):
    with pytest.raises(RampUnavailable, match="pagination not built"):
        records_from(payload, "T")


def test_a_complete_response_with_a_count_is_read():
    assert records_from({"data": [{"id": "x"}], "total": 1, "truncated": False}, "T") == [
        {"id": "x"}
    ]


def test_an_approval_chain_left_out_is_refused_not_read_as_no_approvals():
    chains = [{"entity_id": "a"}]
    assert approval_chains("TRANSACTION", ["a"], chains) == {"a": {"entity_id": "a"}}
    with pytest.raises(RampUnavailable, match="no approval chain returned for 1 TRANSACTION"):
        approval_chains("TRANSACTION", ["a", "b"], chains)


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
        error = RampUnavailable("tool GetBills returned more than one page")
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
    modes = {use.system: use.mode for use in sources.describe()}
    assert modes == {"ramp": "live", "qbo": "local", "bank": "local", "company": "local"}

    ids = [t.id for t in sources.transactions()]
    assert f"ramp:transaction:{DECLINED['id']}" not in ids
    assert len(ids) == 3

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
    assert {p.id for p in sources.people()} == {"ramp:user:u-100", "ramp:user:u-200"}


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
    receipt = {**FIXTURE["receipts"][0], "receipt_url": SIGNED}

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
    error = RampUnavailable(f"tool GetReceipts failed: see {SIGNED} for details")
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
    receipt = {**FIXTURE["receipts"][0], "id": "../../escaped"}
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
    monkeypatch.delenv("RAMP_MCP_URL", raising=False)
    monkeypatch.delenv("RAMP_MCP_TOKEN", raising=False)
    sources = connect_sources(tmp_path / "dataset", cache_dir=tmp_path / "cache")
    assert all(use.mode == "local" for use in sources.describe())


def test_connect_sources_without_credentials_is_local_for_every_system(tmp_path, monkeypatch):
    build_local(tmp_path / "dataset")
    for name in (
        "RAMP_MCP_URL",
        "RAMP_MCP_TOKEN",
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
