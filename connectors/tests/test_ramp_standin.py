"""The Ramp stand-in: a local MCP server over a contract dataset, under Ramp's real tool
names and response shapes. No external network: the server runs on 127.0.0.1 on a free
port for the test module.

Fields that do not survive the round trip (dataset -> stand-in -> connector), and why:
- `raw`: the connector keeps the Ramp-shaped payload the stand-in sent, not the
  dataset record's own `raw`.
- `Person.role`: Ramp's `role` is a permission level, not a job role, so the
  stand-in does not send it and the connector does not read it.
- `approvals`: Ramp's MCP server exposes no approval chain for a transaction, so the
  stand-in serves none and the connector leaves approvals empty (unknown, not unapproved).
- A card's `department`: Ramp's MCP transaction record has none.
- A bill's `department`: Ramp bills have none.
- A card's `due_date` and `invoice_number`: Ramp card transactions have none.
- `Document.path` and `filename`: files are downloaded into the connector's cache.
- Document ids, types and origins come from Ramp's record kinds: anything a card
  names is a `receipt` (external); a bill's invoice is `invoice` (external); any
  other bill attachment is `other` and `internal` (Ramp's FILE, or no type).
- Ids that do not follow the connector's `ramp:<kind>:<ramp id>` scheme come back
  in that scheme.
- Documents that no transaction names are not served.
"""

import dataclasses
import importlib.util
import os
from datetime import UTC, date, datetime
from pathlib import Path

import auditorium_contracts as c
import httpx
import pytest
from auditorium_connectors import RampSources, connect_ramp, connect_sources
from auditorium_connectors.ramp import (
    APPROVALS_UNAVAILABLE,
    DEFAULT_TOOLS,
    STANDIN_SERVER_NAME,
    McpRampClient,
)
from auditorium_connectors.ramp_standin import serving

# Helpers shared with test_ramp.py. Tests run with --import-mode=importlib, so the
# sibling module is loaded by path.
_spec = importlib.util.spec_from_file_location(
    "ramp_helpers", Path(__file__).with_name("test_ramp.py")
)
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
FixtureClient = _helpers.FixtureClient
build_local = _helpers.build_local
minimal_pdf = _helpers.minimal_pdf
payloads = _helpers.payloads

SAM = c.Person(
    id="ramp:user:u-100",
    name="Sam Okafor",
    email="sam@example.com",
    department="Finance",
)
DANA = c.Person(
    id="ramp:user:u-200",
    name="Dana Reyes",
    email="dana@example.com",
    department="Engineering",
    manager_id="ramp:user:u-100",
)
CARD = c.SpendTransaction(
    id="ramp:transaction:c-1",
    kind="card_expense",
    source=c.SourceRef(system="ramp", record_type="transaction", record_id="c-1"),
    vendor_name="Blue Bottle Coffee",
    amount_cents=4250,
    transaction_date=date(2025, 12, 3),
    accounting_date=date(2025, 12, 4),
    paid_date=date(2025, 12, 5),
    memo="Team coffee",
    category="6100 Meals",
    spender_id=DANA.id,
    department="Engineering",
    document_ids=["ramp:receipt:r-900"],
    approvals=[
        c.Approval(
            approver_id=SAM.id,
            decision="approved",
            decided_at=datetime(2025, 12, 4, 10, tzinfo=UTC),
        )
    ],
)
REFUND = c.SpendTransaction(
    id="ramp:transaction:c-2",
    kind="card_expense",
    source=c.SourceRef(system="ramp", record_type="transaction", record_id="c-2"),
    vendor_name="Delta Air Lines",
    amount_cents=-18000,
    transaction_date=date(2025, 12, 10),
    spender_id=DANA.id,
)
BILL = c.SpendTransaction(
    id="ramp:bill:b-10",
    kind="vendor_bill",
    source=c.SourceRef(system="ramp", record_type="bill", record_id="b-10"),
    vendor_name="Northwind Advisory LLC",
    amount_cents=1_200_000,
    transaction_date=date(2026, 1, 8),
    accounting_date=date(2026, 1, 8),
    paid_date=date(2026, 2, 5),
    due_date=date(2026, 2, 7),
    invoice_number="INV-2041",
    memo="Advisory services, December",
    category="6400 Professional Fees",
    spender_id=SAM.id,
    document_ids=["ramp:bill_attachment:att-500", "ramp:bill_attachment:att-501"],
    approvals=[
        c.Approval(
            approver_id=DANA.id, decision="rejected", decided_at=datetime(2026, 1, 9, 8, tzinfo=UTC)
        ),
        c.Approval(
            approver_id=SAM.id, decision="approved", decided_at=datetime(2026, 1, 9, 9, tzinfo=UTC)
        ),
    ],
)
DOCUMENTS = [
    ("ramp:receipt:r-900", "receipt", "receipt", "r-900", "external", "Blue Bottle receipt"),
    (
        "ramp:bill_attachment:att-500",
        "invoice",
        "bill_attachment",
        "att-500",
        "external",
        "INV-2041",
    ),
    (
        "ramp:bill_attachment:att-501",
        "other",
        "bill_attachment",
        "att-501",
        "internal",
        "Approval memo",
    ),
]


def build_dataset(root: Path) -> Path:
    """`build_local` from test_ramp, plus Ramp records and their PDFs."""
    build_local(root)
    documents = c.read_jsonl(root / "documents.jsonl", c.Document)
    for doc_id, doc_type, record_type, record_id, origin, text in DOCUMENTS:
        path = root / "documents" / f"{record_id}.pdf"
        path.write_bytes(minimal_pdf(text))
        documents.append(
            c.Document(
                id=doc_id,
                type=doc_type,
                source=c.SourceRef(system="ramp", record_type=record_type, record_id=record_id),
                origin=origin,
                path=f"documents/{record_id}.pdf",
                sha256=c.sha256_file(path),
                filename=f"{record_id}.pdf",
            )
        )
    c.write_jsonl(root / "documents.jsonl", documents)
    c.write_jsonl(root / "transactions.jsonl", [CARD, REFUND, BILL])
    c.write_jsonl(root / "people.jsonl", [SAM.model_copy(update={"role": "cfo"}), DANA])
    assert c.validate_dataset(root) == []
    return root


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> Path:
    return build_dataset(tmp_path_factory.mktemp("standin") / "dataset")


@pytest.fixture(scope="module")
def standin(dataset) -> str:
    with serving(dataset) as url:
        yield url


@pytest.fixture
def connected(dataset, standin, tmp_path) -> RampSources:
    sources = connect_ramp(
        c.LocalSources(dataset),
        url=standin,
        token="any-token-is-accepted",
        cache_dir=tmp_path / "cache",
    )
    assert isinstance(sources, RampSources), sources.describe()
    return sources


def test_transactions_survive_the_round_trip(connected):
    got = {t.id: t for t in connected.transactions()}
    assert set(got) == {CARD.id, REFUND.id, BILL.id}
    lost = {"raw", "approvals", "department"}
    for original in (CARD, REFUND, BILL):
        assert got[original.id].model_dump(exclude=lost) == original.model_dump(exclude=lost)
        # The MCP path cannot reach approvals: unknown, not "never routed".
        assert got[original.id].approvals is None and got[original.id].department is None
    # The stand-in sends Ramp's shape: a decimal string, not cents.
    assert got[REFUND.id].raw["amount_value"] == "-180.00"
    assert "transaction_uuid" in got[CARD.id].raw


def test_small_pages_read_the_same_records(standin):
    def paged(key, size_argument):
        call = DEFAULT_TOOLS[key]
        arguments = {**call.arguments, size_argument: 1}
        return dataclasses.replace(call, arguments=arguments)

    tools = {
        **DEFAULT_TOOLS,
        "transactions": paged("transactions", "page_size"),
        "bills": paged("bills", "limit"),
        "users": paged("users", "page_size"),
    }
    one_by_one = McpRampClient(standin, "t", tools=tools, receipts_batch=1).fetch()
    assert one_by_one == McpRampClient(standin, "t").fetch()
    assert len(one_by_one.transactions) == 2 and len(one_by_one.users) == 2


def test_people_survive_with_the_job_role_joined_on_email(connected, dataset):
    original = {p.id: p for p in c.LocalSources(dataset).people()}
    got = {p.id: p for p in connected.people()}
    # Ramp has no job role; "cfo" comes back from the company record with Sam's email.
    assert original[SAM.id].role == "cfo"
    assert got[SAM.id] == SAM.model_copy(update={"role": "cfo"})
    assert got[DANA.id] == DANA


def test_documents_download_and_their_sha256_matches(connected, dataset):
    original = {d.id: d for d in c.LocalSources(dataset).documents()}
    ramp_docs = [d for d in connected.documents() if d.source.system is c.SourceSystem.RAMP]
    assert sorted(d.id for d in ramp_docs) == sorted(d[0] for d in DOCUMENTS)
    for document in ramp_docs:
        expected = original[document.id]
        fields = {"id", "type", "source", "origin", "media_type", "sha256"}
        assert document.model_dump(include=fields) == expected.model_dump(include=fields)
        path = connected.document_file(document.id)
        assert path.is_relative_to(connected.cache_dir)
        assert c.sha256_file(path) == expected.sha256
    # The bank statement still comes from the local dataset.
    assert "bank:stmt:2025-12" in {d.id for d in connected.documents()}


def test_the_standin_is_reported_local_and_never_live(connected, standin):
    assert connected.is_standin
    uses = {use.system: use for use in connected.describe()}
    assert all(use.mode == "local" for use in uses.values())
    assert uses["ramp"].detail == (
        f"Ramp stand-in MCP server at {standin}, serving dataset tiny; not Ramp; "
        f"{APPROVALS_UNAVAILABLE}"
    )


def test_connect_sources_against_the_standin_says_local(dataset, standin, tmp_path, monkeypatch):
    monkeypatch.setenv("RAMP_MCP_URL", standin)
    monkeypatch.setenv("RAMP_MCP_TOKEN", "anything")
    monkeypatch.delenv("RAMP_CLIENT_ID", raising=False)
    monkeypatch.delenv("RAMP_CLIENT_SECRET", raising=False)
    for name in [k for k in os.environ if k.startswith("QBO_")]:
        monkeypatch.delenv(name)
    sources = connect_sources(dataset, cache_dir=tmp_path / "cache")
    uses = {use.system: use for use in sources.describe()}
    assert "live" not in {use.mode for use in uses.values()}
    assert "stand-in" in uses["ramp"].detail and "not Ramp" in uses["ramp"].detail
    assert {t.id for t in sources.transactions()} == {CARD.id, REFUND.id, BILL.id}


def test_the_client_reads_the_server_name_from_initialize(standin):
    fetched = McpRampClient(standin, "t").fetch()
    assert fetched.server["name"] == STANDIN_SERVER_NAME


@pytest.mark.parametrize(
    "server",
    [
        None,
        {"name": "ramp-mcp", "version": "1.0"},
        {"name": "Ramp", "title": "auditorium-ramp-standin"},
        {"name": f"{STANDIN_SERVER_NAME}-not", "description": "serving dataset tiny"},
    ],
)
def test_another_server_is_not_taken_for_the_standin(tmp_path, server):
    sources = connect_ramp(
        build_local(tmp_path / "dataset"),
        url="https://demo-mcp.ramp.com/mcp",
        token="t",
        cache_dir=tmp_path / "cache",
        client=FixtureClient(payloads(server=server)),
        download=lambda url, dest: dest.write_bytes(minimal_pdf(url)) and "application/pdf",
    )
    assert isinstance(sources, RampSources)
    assert not sources.is_standin
    ramp = {use.system: use for use in sources.describe()}["ramp"]
    assert ramp.mode == "live"
    assert ramp.detail == f"Ramp MCP https://demo-mcp.ramp.com/mcp; {APPROVALS_UNAVAILABLE}"


def test_unknown_files_are_not_served(standin):
    base = standin.removesuffix("/mcp")
    assert httpx.get(f"{base}/files/receipt/nope").status_code == 404
    assert httpx.get(f"{base}/files/receipt/r-900").headers["content-type"] == "application/pdf"


def test_the_standin_is_reported_local_and_synthetic(connected):
    use = next(u for u in connected.describe() if u.system is c.SourceSystem.RAMP)
    assert use.mode == "local" and use.synthetic is True
