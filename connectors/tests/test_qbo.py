"""QuickBooks connector tests. No network: HTTP goes through httpx.MockTransport.

The payloads below are DOCUMENTED-SHAPE fixtures, written from Intuit's published
Accounting API reference (Bill, Purchase, JournalEntry, query and Fault responses).
They were not captured from a real sandbox. Replace them with captured payloads
once credentials exist.
"""

import json
from datetime import date

import httpx
import pytest
from auditorium_connectors import qbo
from auditorium_contracts import (
    DatasetManifest,
    LedgerEntry,
    LedgerLine,
    Person,
    SourceRef,
    SpendPolicy,
    write_json,
    write_jsonl,
)

# --- documented-shape fixtures --------------------------------------------------

BILL = {
    "Id": "147",
    "SyncToken": "0",
    "DocNumber": "INV-2041",
    "TxnDate": "2026-01-08",
    "DueDate": "2026-02-07",
    "PrivateNote": "December advisory work",
    "VendorRef": {"value": "56", "name": "Northwind Advisory LLC"},
    "APAccountRef": {"value": "33", "name": "Accounts Payable (A/P)"},
    "TotalAmt": 12000.0,
    "Balance": 12000.0,
    "CurrencyRef": {"value": "USD", "name": "United States Dollar"},
    "Line": [
        {
            "Id": "1",
            "LineNum": 1,
            "Description": "Advisory services",
            "Amount": 12000.0,
            "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": "7", "name": "Legal & Professional Fees"},
                "BillableStatus": "NotBillable",
                "TaxCodeRef": {"value": "NON"},
            },
        }
    ],
    "MetaData": {
        "CreateTime": "2026-01-08T10:00:00-08:00",
        "LastUpdatedTime": "2026-01-08T10:00:00-08:00",
    },
}

PURCHASE = {
    "Id": "252",
    "SyncToken": "0",
    "TxnDate": "2025-12-03",
    "PaymentType": "CreditCard",
    "AccountRef": {"value": "41", "name": "Ramp Card"},
    "EntityRef": {"value": "12", "name": "Figma", "type": "Vendor"},
    "DepartmentRef": {"value": "2", "name": "Design"},
    "TotalAmt": 45.99,
    "Line": [
        {
            "Id": "1",
            "Amount": 45.99,
            "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {"value": "9", "name": "Software"},
            },
        },
        {
            "Id": "2",
            "Amount": 0.0,
            "DetailType": "ItemBasedExpenseLineDetail",
            "ItemBasedExpenseLineDetail": {"ItemRef": {"value": "3", "name": "Seat"}},
        },
    ],
}

REFUND = {
    "Id": "253",
    "TxnDate": "2025-12-10",
    "PaymentType": "CreditCard",
    "Credit": True,
    "AccountRef": {"value": "41", "name": "Ramp Card"},
    "EntityRef": {"value": "12", "name": "Figma", "type": "Vendor"},
    "TotalAmt": 45.99,
    "Line": [
        {
            "Amount": 45.99,
            "DetailType": "AccountBasedExpenseLineDetail",
            "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "9", "name": "Software"}},
        }
    ],
}

JOURNAL = {
    "Id": "301",
    "TxnDate": "2025-12-31",
    "DocNumber": "ACCR-12",
    "PrivateNote": "Accrue December advisory\nauditorium-ref: qbo:JournalEntry:ds-1",
    "Adjustment": False,
    "TotalAmt": 12000.0,
    "Line": [
        {
            "Id": "0",
            "Amount": 12000.0,
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {
                "PostingType": "Debit",
                "AccountRef": {"value": "7", "name": "Legal & Professional Fees"},
                "Entity": {
                    "Type": "Vendor",
                    "EntityRef": {"value": "56", "name": "Northwind Advisory LLC"},
                },
            },
        },
        {
            "Id": "1",
            "Amount": 12000.0,
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {
                "PostingType": "Credit",
                "AccountRef": {"value": "88", "name": "Accrued Liabilities"},
                "Entity": {
                    "Type": "Vendor",
                    "EntityRef": {"value": "56", "name": "Northwind Advisory LLC"},
                },
            },
        },
    ],
}

ENV = {
    "QBO_ENVIRONMENT": "sandbox",
    "QBO_CLIENT_ID": "client-id",
    "QBO_CLIENT_SECRET": "client-secret",
    "QBO_REALM_ID": "9130000000000",
    "QBO_REFRESH_TOKEN": "refresh-original",
}


# --- mapping ------------------------------------------------------------------


def test_bill_maps_to_ledger_entry_with_raw_kept():
    entry = qbo.map_entity("Bill", BILL)
    assert entry.id == "qbo:Bill:147"
    assert entry.source == SourceRef(system="qbo", record_type="Bill", record_id="147")
    assert entry.entry_type == "bill"
    assert entry.txn_date == date(2026, 1, 8)
    assert entry.vendor_name == "Northwind Advisory LLC"
    assert entry.doc_number == "INV-2041"
    assert entry.memo == "December advisory work"
    assert entry.total_cents == 1_200_000
    assert entry.lines == [
        LedgerLine(
            account="Legal & Professional Fees",
            side="debit",
            amount_cents=1_200_000,
            description="Advisory services",
        ),
        LedgerLine(account="Accounts Payable (A/P)", side="credit", amount_cents=1_200_000),
    ]
    assert entry.raw == BILL


def test_bill_without_ap_account_gets_no_invented_credit_line():
    bill = {k: v for k, v in BILL.items() if k != "APAccountRef"}
    entry = qbo.map_entity("Bill", bill)
    assert [line.side for line in entry.lines] == ["debit"]


def test_purchase_maps_card_expense_and_leaves_item_lines_in_raw():
    entry = qbo.map_entity("Purchase", PURCHASE)
    assert entry.id == "qbo:Purchase:252"
    assert entry.entry_type == "expense"
    assert entry.vendor_name == "Figma"
    assert entry.total_cents == 4599
    assert entry.doc_number is None and entry.memo is None
    assert [(line.account, line.side, line.amount_cents) for line in entry.lines] == [
        ("Software", "debit", 4599),
        ("Ramp Card", "credit", 4599),
    ]
    assert {line.department for line in entry.lines} == {"Design"}
    assert entry.raw["Line"][1]["DetailType"] == "ItemBasedExpenseLineDetail"


def test_purchase_credit_is_a_negative_refund_with_sides_flipped():
    entry = qbo.map_entity("Purchase", REFUND)
    assert entry.total_cents == -4599
    assert [(line.account, line.side) for line in entry.lines] == [
        ("Software", "credit"),
        ("Ramp Card", "debit"),
    ]


def test_purchase_paid_to_a_customer_has_no_vendor():
    purchase = {**PURCHASE, "EntityRef": {"value": "3", "name": "Acme", "type": "Customer"}}
    assert qbo.map_entity("Purchase", purchase).vendor_name is None


def test_journal_entry_maps_lines_vendor_and_strips_loader_ref_from_memo():
    entry = qbo.map_entity("JournalEntry", JOURNAL)
    assert entry.id == "qbo:JournalEntry:301"
    assert entry.entry_type == "journal_entry"
    assert entry.vendor_name == "Northwind Advisory LLC"
    assert entry.memo == "Accrue December advisory"
    assert entry.raw["PrivateNote"].endswith("auditorium-ref: qbo:JournalEntry:ds-1")
    assert qbo.loaded_ref(JOURNAL) == "qbo:JournalEntry:ds-1"
    assert entry.total_cents == 1_200_000
    assert [(line.account, line.side) for line in entry.lines] == [
        ("Legal & Professional Fees", "debit"),
        ("Accrued Liabilities", "credit"),
    ]


def test_journal_entry_with_two_vendors_has_no_header_vendor():
    journal = json.loads(json.dumps(JOURNAL))
    journal["Line"][1]["JournalEntryLineDetail"]["Entity"]["EntityRef"]["name"] = "Other Co"
    assert qbo.map_entity("JournalEntry", journal).vendor_name is None


def test_amounts_become_exact_cents():
    assert qbo.to_cents(0.1 + 0.2) == 30
    assert qbo.to_cents(1234.56) == 123456
    assert qbo.to_cents("19.99") == 1999


# --- loader planning ------------------------------------------------------------


def entry(**overrides):
    base = dict(
        id="qbo:Bill:147",
        source=SourceRef(system="qbo", record_type="Bill", record_id="147"),
        entry_type="bill",
        txn_date=date(2026, 1, 8),
        vendor_name="Northwind Advisory LLC",
        doc_number="INV-2041",
        memo="December advisory work",
        total_cents=1_200_000,
        lines=[
            LedgerLine(account="Legal & Professional Fees", side="debit", amount_cents=1_200_000),
            LedgerLine(account="Accounts Payable (A/P)", side="credit", amount_cents=1_200_000),
        ],
    )
    return LedgerEntry(**{**base, **overrides})


def test_plan_bill_stamps_reference_and_uses_ap_line():
    plan = qbo.plan_entry(entry())
    assert plan.entity == "Bill"
    assert plan.payload["PrivateNote"] == "December advisory work\nauditorium-ref: qbo:Bill:147"
    assert plan.payload["VendorRef"] == {"name": "Northwind Advisory LLC"}
    assert plan.payload["APAccountRef"] == {"name": "Accounts Payable (A/P)"}
    assert plan.payload["Line"][0]["Amount"] == 12000.0
    assert plan.accounts == {"Legal & Professional Fees": "expense", "Accounts Payable (A/P)": "ap"}


def test_mapping_a_planned_bill_round_trips():
    plan = qbo.plan_entry(entry())
    payload = json.loads(json.dumps(plan.payload))
    payload["Id"] = "900"
    payload["TotalAmt"] = 12000.0
    back = qbo.map_entity("Bill", payload)
    original = entry()
    assert (back.txn_date, back.vendor_name, back.doc_number, back.memo, back.total_cents) == (
        original.txn_date,
        original.vendor_name,
        original.doc_number,
        original.memo,
        original.total_cents,
    )
    assert [(x.account, x.side, x.amount_cents) for x in back.lines] == [
        (x.account, x.side, x.amount_cents) for x in original.lines
    ]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"lines": []}, "no lines"),
        ({"entry_type": "bill_payment"}, "not loaded"),
        ({"total_cents": 1_000_000}, "do not add up"),
        ({"vendor_name": None}, "needs a vendor"),
        ({"doc_number": "X" * 22}, "21 characters"),
    ],
)
def test_plan_refuses_rather_than_invents(overrides, reason):
    with pytest.raises(qbo.Skip, match=reason):
        qbo.plan_entry(entry(**overrides))


def test_plan_card_refund_sets_credit():
    refund = entry(
        id="qbo:Purchase:9",
        entry_type="expense",
        total_cents=-4599,
        doc_number=None,
        lines=[
            LedgerLine(account="Software", side="credit", amount_cents=4599),
            LedgerLine(account="Ramp Card", side="debit", amount_cents=4599),
        ],
    )
    plan = qbo.plan_entry(refund)
    assert plan.entity == "Purchase"
    assert plan.payload["Credit"] is True
    assert plan.payload["AccountRef"] == {"name": "Ramp Card"}


def test_account_type_guess():
    assert qbo.guess_account_type("Accrued Liabilities", "journal") == "Other Current Liability"
    assert qbo.guess_account_type("Ramp Card", "payment") == "Credit Card"
    assert qbo.guess_account_type("Software", "expense") == "Expense"


# --- config and the sandbox guard -------------------------------------------------


def test_config_names_missing_vars_without_values():
    with pytest.raises(qbo.QboConfigError) as error:
        qbo.QboConfig.from_env({"QBO_ENVIRONMENT": "sandbox", "QBO_CLIENT_SECRET": "s3cr3t"})
    message = str(error.value)
    assert "QBO_CLIENT_ID" in message and "QBO_REFRESH_TOKEN" in message
    assert "s3cr3t" not in message


def test_config_repr_hides_secrets():
    config = qbo.QboConfig.from_env(ENV)
    assert "client-secret" not in repr(config) and "refresh-original" not in repr(config)


def test_loader_refuses_production(tmp_path):
    config = qbo.QboConfig.from_env({**ENV, "QBO_ENVIRONMENT": "production"})
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500))
    client = qbo.QboClient(config, httpx.Client(transport=transport))
    with pytest.raises(qbo.QboConfigError, match="sandbox"):
        qbo.load_dataset_ledger(tmp_path, client)
    assert calls == []


# --- live path through a mock transport -----------------------------------------


class FakeQbo:
    """A sandbox stand-in: answers the token endpoint, queries and creates."""

    def __init__(self, store=None, rotate_to=None):
        self.store = store or {}
        self.rotate_to = rotate_to
        self.requests = []
        self.next_id = 1000

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth.platform.intuit.com":
            body = {"access_token": "access-1", "expires_in": 3600}
            if self.rotate_to:
                body["refresh_token"] = self.rotate_to
            return httpx.Response(200, json=body)
        assert request.url.host == "sandbox-quickbooks.api.intuit.com"
        assert request.headers["Authorization"] == "Bearer access-1"
        assert request.url.params["minorversion"] == "75"
        if request.url.path.endswith("/query"):
            entity = request.url.params["query"].split()[3]
            return httpx.Response(200, json={"QueryResponse": {entity: self.store.get(entity, [])}})
        entity = {
            "bill": "Bill",
            "purchase": "Purchase",
            "journalentry": "JournalEntry",
            "vendor": "Vendor",
            "account": "Account",
        }[request.url.path.rsplit("/", 1)[-1]]
        body = json.loads(request.content)
        body["Id"] = str(self.next_id)
        self.next_id += 1
        self.store.setdefault(entity, []).append(body)
        return httpx.Response(200, json={entity: body})


def make_client(fake, tmp_path, env=ENV):
    config = qbo.QboConfig.from_env({**env, "QBO_TOKEN_CACHE": str(tmp_path / "token.json")})
    return qbo.QboClient(config, httpx.Client(transport=httpx.MockTransport(fake)))


def test_fetch_ledger_reads_all_three_entities(tmp_path):
    fake = FakeQbo({"Bill": [BILL], "Purchase": [PURCHASE], "JournalEntry": [JOURNAL]})
    entries = make_client(fake, tmp_path).fetch_ledger()
    assert [e.id for e in entries] == ["qbo:Bill:147", "qbo:Purchase:252", "qbo:JournalEntry:301"]


def test_rotated_refresh_token_is_cached_outside_env_and_tied_to_its_seed(tmp_path):
    fake = FakeQbo(rotate_to="refresh-rotated")
    make_client(fake, tmp_path).fetch_ledger()
    assert make_client(FakeQbo(), tmp_path)._refresh_token == "refresh-rotated"
    reseeded = {**ENV, "QBO_REFRESH_TOKEN": "refresh-new-from-playground"}
    assert (
        make_client(FakeQbo(), tmp_path, reseeded)._refresh_token == "refresh-new-from-playground"
    )


def write_dataset(root, ledger):
    root.mkdir(parents=True, exist_ok=True)
    write_json(
        root / "manifest.json",
        DatasetManifest(
            dataset_id="fixture",
            company_name="Larkspur Labs",
            contract_version="0.1.0",
            fiscal_year_start=date(2025, 1, 1),
            fiscal_year_end=date(2025, 12, 31),
            coverage_end=date(2026, 2, 28),
        ),
    )
    write_jsonl(root / "ledger.jsonl", ledger)
    for name in ("transactions.jsonl", "documents.jsonl"):
        (root / name).write_text("")
    write_jsonl(root / "people.jsonl", [Person(id="p1", name="Dana Reyes")])
    write_json(
        root / "policy.json",
        SpendPolicy(
            id="policy",
            effective_from=date(2025, 1, 1),
            receipt_required_from_cents=7500,
            approval_rules=[],
        ),
    )


def test_load_creates_vendors_accounts_and_entries_once(tmp_path):
    dataset = tmp_path / "dataset"
    accrual = entry(
        id="qbo:JournalEntry:ds-1",
        entry_type="journal_entry",
        txn_date=date(2025, 12, 31),
        doc_number="ACCR-12",
        lines=[
            LedgerLine(account="Legal & Professional Fees", side="debit", amount_cents=1_200_000),
            LedgerLine(account="Accrued Liabilities", side="credit", amount_cents=1_200_000),
        ],
    )
    write_dataset(dataset, [entry(), accrual, entry(id="qbo:Bill:empty", lines=[])])
    fake = FakeQbo(
        {
            "Account": [
                {"Id": "33", "Name": "Accounts Payable (A/P)", "AccountType": "Accounts Payable"}
            ]
        }
    )
    client = make_client(fake, tmp_path)

    first = qbo.load_dataset_ledger(dataset, client)
    assert first.created == ["qbo:Bill:1002", "qbo:JournalEntry:1004"]
    assert first.skipped == {
        "qbo:Bill:empty": "no lines; QuickBooks needs at least one account line"
    }
    assert first.new_vendors == ["Northwind Advisory LLC"]
    assert first.new_accounts == ["Legal & Professional Fees", "Accrued Liabilities"]
    bill = fake.store["Bill"][0]
    assert bill["VendorRef"] == {"value": "1001"}
    assert bill["APAccountRef"] == {"value": "33"}
    created_accrued = next(a for a in fake.store["Account"] if a["Name"] == "Accrued Liabilities")
    assert created_accrued["AccountType"] == "Other Current Liability"

    second = qbo.load_dataset_ledger(dataset, client)
    assert second.created == []
    assert second.already_loaded == ["qbo:Bill:147", "qbo:JournalEntry:ds-1"]
    assert len(fake.store["Bill"]) == 1


# --- QboLedger: live or an honest fallback ----------------------------------------


def test_qbo_ledger_live(tmp_path):
    fake = FakeQbo({"Bill": [BILL]})
    ledger = qbo.QboLedger(client=make_client(fake, tmp_path))
    use = ledger.describe()
    assert use.system == "qbo" and use.mode == "live"
    assert "sandbox-quickbooks.api.intuit.com" in use.detail
    assert [e.id for e in ledger.ledger_entries()] == ["qbo:Bill:147"]


def test_qbo_ledger_falls_back_when_not_configured(tmp_path):
    dataset = tmp_path / "dataset"
    write_dataset(dataset, [entry()])
    ledger = qbo.QboLedger(fallback=dataset, env={})
    use = ledger.describe()
    assert use.mode == "local"
    assert "QBO_CLIENT_ID" in use.detail and "fixture" in use.detail
    assert [e.id for e in ledger.ledger_entries()] == ["qbo:Bill:147"]


def test_qbo_ledger_falls_back_when_qbo_refuses(tmp_path):
    dataset = tmp_path / "dataset"
    write_dataset(dataset, [entry()])

    def refuse(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    config = qbo.QboConfig.from_env({**ENV, "QBO_TOKEN_CACHE": str(tmp_path / "t.json")})
    client = qbo.QboClient(config, httpx.Client(transport=httpx.MockTransport(refuse)))
    use = qbo.QboLedger(fallback=dataset, client=client).describe()
    assert use.mode == "local"
    assert "invalid_grant" in use.detail
    assert "refresh-original" not in use.detail and "client-secret" not in use.detail


def test_qbo_ledger_without_fallback_raises(tmp_path):
    with pytest.raises(qbo.QboError, match="no local fallback"):
        qbo.QboLedger(env={}).ledger_entries()


def test_cli_dry_run_needs_no_credentials(tmp_path, capsys):
    dataset = tmp_path / "dataset"
    write_dataset(dataset, [entry(), entry(id="qbo:Bill:empty", lines=[])])
    assert qbo.main(["load", str(dataset), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would load 1, skip 1" in out
