"""Ramp mapping and fallback. No network: the payloads are synthetic fixtures shaped
by Ramp's Developer API spec (see the `_synthetic` note in the fixture file).

One live test runs only with AUDITORIUM_LIVE_RAMP=1, RAMP_MCP_URL and RAMP_MCP_TOKEN set.
"""

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import auditorium_contracts as c
import pytest
from auditorium_connectors import LocalFallback, RampSources, connect_ramp, connect_sources
from auditorium_connectors.ramp import (
    McpRampClient,
    RampMappingError,
    RampPayloads,
    RampUnavailable,
    map_bill,
    map_card_transaction,
    map_user,
    records_from,
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


def fake_download(url: str, dest: Path) -> str:
    dest.write_text(f"file behind {url}\n")
    return "application/pdf"


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
def test_live_ramp_loads():
    client = McpRampClient(os.environ["RAMP_MCP_URL"], os.environ["RAMP_MCP_TOKEN"])
    result = client.fetch()
    assert result.transactions or result.bills
