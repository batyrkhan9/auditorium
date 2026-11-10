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
    # QuickBooks puts the department on the entry; the lines set none of their own.
    assert entry.department == "Design"
    assert {line.department for line in entry.lines} == {None}
    assert entry.payment_type == "CreditCard"
    assert entry.raw["Line"][1]["DetailType"] == "ItemBasedExpenseLineDetail"


ACCOUNTS = [
    {"Id": "7", "Name": "Legal & Professional Fees", "AccountType": "Expense"},
    {"Id": "33", "Name": "Accounts Payable (A/P)", "AccountType": "Accounts Payable"},
    {"Id": "88", "Name": "Accrued Liabilities", "AccountType": "Other Current Liability"},
    {"Id": "9", "Name": "Software", "AccountType": "Expense"},
    {"Id": "41", "Name": "Ramp Card", "AccountType": "Credit Card"},
]


def test_lines_carry_quickbooks_account_types(tmp_path):
    fake = FakeQbo({"Account": ACCOUNTS, "Bill": [BILL], "JournalEntry": [JOURNAL]})
    client = make_client(fake, tmp_path)
    bill, journal = client.fetch_ledger()
    assert [(x.account, x.account_type) for x in bill.lines] == [
        ("Legal & Professional Fees", "Expense"),
        ("Accounts Payable (A/P)", "Accounts Payable"),
    ]
    assert [x.account_type for x in journal.lines] == ["Expense", "Other Current Liability"]
    # The accounts are read once per client, not once per fetch.
    client.fetch_ledger()
    queries = [r.url.params.get("query", "") for r in fake.requests]
    assert sum(" FROM Account " in q for q in queries) == 1


def test_an_account_quickbooks_did_not_list_gets_no_type():
    entry = qbo.map_entity("Bill", BILL, {"id:7": "Expense"})
    assert [x.account_type for x in entry.lines] == ["Expense", None]


def test_journal_lines_name_their_vendor_and_only_their_own_department():
    journal = json.loads(json.dumps(JOURNAL))
    journal["DepartmentRef"] = {"value": "1", "name": "Finance"}
    journal["Line"][0]["JournalEntryLineDetail"]["DepartmentRef"] = {"name": "Legal"}
    entry = qbo.map_entity("JournalEntry", journal)
    assert [x.vendor_name for x in entry.lines] == ["Northwind Advisory LLC"] * 2
    assert entry.department == "Finance"
    assert [x.department for x in entry.lines] == ["Legal", None]
    assert entry.payment_type is None


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
    assert plan.payload["PaymentType"] == "CreditCard"
    assert plan.payload["AccountRef"] == {"name": "Ramp Card"}


def card_expense(**overrides):
    return entry(
        **{
            "id": "qbo:Purchase:ds-7",
            "entry_type": "expense",
            "vendor_name": "Figma",
            "doc_number": None,
            "memo": None,
            "total_cents": 4599,
            "lines": [
                LedgerLine(account="Software", side="debit", amount_cents=4599),
                LedgerLine(account="Ramp Card", side="credit", amount_cents=4599),
            ],
            **overrides,
        }
    )


def test_plan_expense_takes_payment_type_from_the_entry():
    paid_by_check = card_expense(
        payment_type="Check",
        lines=[
            LedgerLine(account="Software", side="debit", amount_cents=4599),
            LedgerLine(account="Operating", side="credit", amount_cents=4599),
        ],
    )
    plan = qbo.plan_entry(paid_by_check)
    assert plan.payload["PaymentType"] == "Check"
    assert qbo.guess_account_type("Operating", plan.accounts["Operating"]) == "Bank"


def test_plan_expense_refuses_to_guess_cash_or_check():
    unknown = card_expense(
        lines=[
            LedgerLine(account="Software", side="debit", amount_cents=4599),
            LedgerLine(account="Operating", side="credit", amount_cents=4599),
        ]
    )
    with pytest.raises(qbo.Skip, match="cannot tell how the expense was paid"):
        qbo.plan_entry(unknown)


def test_plan_expense_does_not_read_payment_type_from_raw():
    unknown = card_expense(
        raw={"PaymentType": "Check"},
        lines=[
            LedgerLine(account="Software", side="debit", amount_cents=4599),
            LedgerLine(account="Operating", side="credit", amount_cents=4599),
        ],
    )
    with pytest.raises(qbo.Skip, match="cannot tell how the expense was paid"):
        qbo.plan_entry(unknown)


def test_plan_expense_refuses_a_paid_from_account_type_that_contradicts_payment_type():
    contradicts = card_expense(
        payment_type="CreditCard",
        lines=[
            LedgerLine(account="Software", side="debit", amount_cents=4599),
            LedgerLine(account="Ramp Card", account_type="Bank", side="credit", amount_cents=4599),
        ],
    )
    with pytest.raises(qbo.Skip, match="is a Bank account; PaymentType CreditCard"):
        qbo.plan_entry(contradicts)


def test_plan_takes_account_types_from_the_dataset_over_a_guess():
    accrual = entry(
        id="qbo:JournalEntry:ds-1",
        entry_type="journal_entry",
        lines=[
            LedgerLine(account="Advisory", account_type="Expense", side="debit", amount_cents=100),
            # By name alone this would be guessed an Expense account.
            LedgerLine(
                account="Year-end",
                account_type="Other Current Liability",
                side="credit",
                amount_cents=100,
                vendor_name="Line Vendor",
            ),
        ],
    )
    plan = qbo.plan_entry(accrual)
    assert plan.account_type("Year-end") == "Other Current Liability"
    assert qbo.guess_account_type("Year-end", "journal") == "Expense"
    entities = [x["JournalEntryLineDetail"]["Entity"]["EntityRef"] for x in plan.payload["Line"]]
    assert entities == [{"name": "Northwind Advisory LLC"}, {"name": "Line Vendor"}]


def test_plan_refund_needs_a_card_payment():
    refund = card_expense(
        total_cents=-4599,
        payment_type="Cash",
        lines=[
            LedgerLine(account="Software", side="credit", amount_cents=4599),
            LedgerLine(account="Petty Cash", side="debit", amount_cents=4599),
        ],
    )
    with pytest.raises(qbo.Skip, match="only valid on a CreditCard expense"):
        qbo.plan_entry(refund)


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
        if entity == "Purchase":
            # QuickBooks requires PaymentType, and Credit only goes with CreditCard.
            kind = body.get("PaymentType")
            if kind not in ("Cash", "Check", "CreditCard") or (
                body.get("Credit") and kind != "CreditCard"
            ):
                fault = {"Error": [{"Message": "Invalid PaymentType", "Detail": str(kind)}]}
                return httpx.Response(400, json={"Fault": fault})
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


def test_token_cache_is_private_from_creation_and_leaves_no_temp_file(tmp_path):
    cache = tmp_path / "cache" / "auditorium" / "token.json"
    config = qbo.QboConfig.from_env({**ENV, "QBO_TOKEN_CACHE": str(cache)})
    client = qbo.QboClient(config, httpx.Client(transport=httpx.MockTransport(FakeQbo())))
    client._store_refresh_token("refresh-rotated")
    client._store_refresh_token("refresh-rotated-again")
    assert cache.stat().st_mode & 0o777 == 0o600
    assert cache.parent.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in cache.parent.iterdir()] == ["token.json"]
    assert json.loads(cache.read_text())["refresh_token"] == "refresh-rotated-again"


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
            synthetic=True,
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


def test_load_creates_a_card_expense_with_its_payment_type(tmp_path):
    dataset = tmp_path / "dataset"
    write_dataset(dataset, [card_expense()])
    fake = FakeQbo()
    report = qbo.load_dataset_ledger(dataset, make_client(fake, tmp_path))
    assert report.failed == {} and report.skipped == {}
    assert report.created == ["qbo:Purchase:1003"]
    purchase = fake.store["Purchase"][0]
    assert purchase["PaymentType"] == "CreditCard"
    card = next(a for a in fake.store["Account"] if a["Name"] == "Ramp Card")
    assert card["AccountType"] == "Credit Card"
    assert purchase["AccountRef"] == {"value": card["Id"]}


def test_load_skips_an_expense_whose_paid_from_account_has_another_type(tmp_path):
    dataset = tmp_path / "dataset"
    write_dataset(dataset, [card_expense()])
    fake = FakeQbo({"Account": [{"Id": "41", "Name": "Ramp Card", "AccountType": "Bank"}]})
    report = qbo.load_dataset_ledger(dataset, make_client(fake, tmp_path))
    assert report.created == []
    assert "is a Bank account in QuickBooks" in report.skipped["qbo:Purchase:ds-7"]
    assert "Purchase" not in fake.store


# --- QboLedger: live or an honest fallback ----------------------------------------


def test_qbo_ledger_live(tmp_path):
    fake = FakeQbo({"Bill": [BILL]})
    ledger = qbo.QboLedger(client=make_client(fake, tmp_path))
    use = ledger.describe()
    assert use.system == "qbo" and use.mode == "live"
    assert use.synthetic is True  # a sandbox company holds only test data
    assert "sandbox-quickbooks.api.intuit.com" in use.detail
    assert "read Bill, Purchase, JournalEntry" in use.detail
    assert [e.id for e in ledger.ledger_entries()] == ["qbo:Bill:147"]


def test_qbo_ledger_live_says_how_much_of_the_dataset_is_missing(tmp_path):
    dataset = tmp_path / "dataset"
    write_dataset(dataset, [entry(id="qbo:JournalEntry:ds-1"), entry(id="qbo:Bill:ds-2")])
    # One loaded entry (JOURNAL carries the stamp) and one of Intuit's own.
    fake = FakeQbo({"Bill": [BILL], "JournalEntry": [JOURNAL]})
    ledger = qbo.QboLedger(fallback=dataset, client=make_client(fake, tmp_path))
    use = ledger.describe()
    assert use.mode == "live"
    assert "dataset fixture: 1 of 2 entries missing, 1 entries not from the dataset" in use.detail
    assert [e.id for e in ledger.ledger_entries()] == ["qbo:Bill:147", "qbo:JournalEntry:301"]


def test_qbo_ledger_with_none_of_the_dataset_loaded_is_local(tmp_path):
    dataset = tmp_path / "dataset"
    write_dataset(dataset, [entry()])
    # Only Intuit's sample company: nothing the loader stamped.
    fake = FakeQbo({"Bill": [BILL], "Purchase": [PURCHASE]})
    ledger = qbo.QboLedger(fallback=dataset, client=make_client(fake, tmp_path))
    use = ledger.describe()
    assert use.mode == "local"
    assert "not live: the sandbox holds none of the 1 ledger entries of dataset fixture" in (
        use.detail
    )
    assert "2 entries read, 0 stamped" in use.detail
    assert [e.id for e in ledger.ledger_entries()] == ["qbo:Bill:147"]  # the dataset's one


def test_qbo_ledger_falls_back_when_not_configured(tmp_path):
    dataset = tmp_path / "dataset"
    write_dataset(dataset, [entry()])
    ledger = qbo.QboLedger(fallback=dataset, env={})
    use = ledger.describe()
    assert use.mode == "local"
    assert use.synthetic is True  # the fixture manifest says so
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
    assert "plan: 1 entries, skip 1; QuickBooks not checked" in out
    assert "would load" not in out


def test_load_skips_an_entry_whose_account_quickbooks_holds_with_another_type(tmp_path):
    dataset = tmp_path / "dataset"
    depreciation = entry(
        id="qbo:JournalEntry:ds-9",
        entry_type="journal_entry",
        vendor_name=None,
        lines=[
            LedgerLine(
                account="Depreciation", account_type="Expense", side="debit", amount_cents=100
            ),
            LedgerLine(
                account="Accumulated Depreciation",
                account_type="Fixed Asset",
                side="credit",
                amount_cents=100,
            ),
        ],
    )
    write_dataset(dataset, [depreciation])
    held = {"Id": "60", "Name": "Accumulated Depreciation", "AccountType": "Expense"}
    fake = FakeQbo({"Account": [held]})
    report = qbo.load_dataset_ledger(dataset, make_client(fake, tmp_path))
    assert report.created == []
    assert report.skipped["qbo:JournalEntry:ds-9"] == (
        "'Accumulated Depreciation' is a Expense account in QuickBooks; the dataset says "
        "Fixed Asset"
    )


# --- repairing account types the old loader guessed --------------------------------


class UpdatingQbo(FakeQbo):
    """FakeQbo that also takes sparse updates (a POST whose body has an Id), checks
    SyncToken, lists only active accounts and can refuse an AccountType change."""

    PATHS = {"account": "Account", "journalentry": "JournalEntry"}

    def __init__(self, store=None, refuse_retype=False):
        super().__init__(store)
        self.refuse_retype = refuse_retype

    def writes(self):
        return [
            r for r in self.requests if r.method == "POST" and "intuit.com/oauth" not in str(r.url)
        ]

    def __call__(self, request):
        if request.url.path.endswith("/query") and "FROM Account" in request.url.params["query"]:
            self.requests.append(request)
            active = [a for a in self.store.get("Account", []) if a.get("Active", True)]
            return httpx.Response(200, json={"QueryResponse": {"Account": active}})
        api = request.url.host == "sandbox-quickbooks.api.intuit.com"
        body = json.loads(request.content) if api and request.method == "POST" else {}
        if "Id" not in body:
            return super().__call__(request)
        self.requests.append(request)
        entity = self.PATHS[request.url.path.rsplit("/", 1)[-1]]
        record = next(r for r in self.store[entity] if r["Id"] == body["Id"])
        fault = None
        if not body.get("sparse") or body["SyncToken"] != record["SyncToken"]:
            fault = "stale or full update"
        elif self.refuse_retype and body.get("AccountType", record.get("AccountType")) != (
            record.get("AccountType")
        ):
            fault = "Account type cannot be changed for an account with transactions"
        if fault:
            return httpx.Response(400, json={"Fault": {"Error": [{"Message": fault}]}})
        record.update({k: v for k, v in body.items() if k != "sparse"})
        record["SyncToken"] = str(int(record["SyncToken"]) + 1)
        return httpx.Response(200, json={entity: record})


def depreciation_entry(n):
    return entry(
        id=f"qbo:JournalEntry:dep-{n}",
        entry_type="journal_entry",
        vendor_name=None,
        doc_number=None,
        memo=None,
        total_cents=315_000,
        lines=[
            LedgerLine(
                account="Depreciation Expense",
                account_type="Expense",
                side="debit",
                amount_cents=315_000,
            ),
            LedgerLine(
                account="Accumulated Depreciation",
                account_type="Fixed Asset",
                side="credit",
                amount_cents=315_000,
            ),
        ],
    )


def depreciation_journal(record_id, ref, account_id="900"):
    note = f"{qbo.REF_PREFIX}{ref}" if ref else "Intuit sample"
    return {
        "Id": record_id,
        "SyncToken": "0",
        "TxnDate": "2025-01-31",
        "PrivateNote": note,
        "Line": [
            {
                "Id": "0",
                "Amount": 3150.0,
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": {
                    "PostingType": "Debit",
                    "AccountRef": {"value": "901", "name": "Depreciation Expense"},
                },
            },
            {
                "Id": "1",
                "Amount": 3150.0,
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": {
                    "PostingType": "Credit",
                    "AccountRef": {"value": account_id, "name": "Accumulated Depreciation"},
                },
            },
        ],
    }


def depreciation_sandbox(journals, refuse_retype=False):
    accounts = [
        {
            "Id": "900",
            "SyncToken": "0",
            "Name": "Accumulated Depreciation",
            "AccountType": "Expense",
            "AccountSubType": "Travel",
        },
        {
            "Id": "901",
            "SyncToken": "0",
            "Name": "Depreciation Expense",
            "AccountType": "Expense",
            "AccountSubType": "Travel",
        },
    ]
    return UpdatingQbo({"Account": accounts, "JournalEntry": journals}, refuse_retype)


def repair_fixture(tmp_path, journals, refuse_retype=False):
    dataset = tmp_path / "dataset"
    write_dataset(dataset, [depreciation_entry(1), depreciation_entry(2)])
    fake = depreciation_sandbox(journals, refuse_retype)
    return dataset, fake, make_client(fake, tmp_path)


STAMPED = [
    ("j1", "qbo:JournalEntry:dep-1"),
    ("j2", "qbo:JournalEntry:dep-2"),
]


def test_repair_dry_run_reports_and_writes_nothing(tmp_path):
    dataset, fake, client = repair_fixture(tmp_path, [depreciation_journal(*j) for j in STAMPED])
    report = qbo.repair_account_types(dataset, client)
    assert list(report.planned) == ["Accumulated Depreciation"]
    assert "Expense/Travel -> Fixed Asset" in report.planned["Accumulated Depreciation"]
    assert "2 stamped entries" in report.planned["Accumulated Depreciation"]
    assert report.fixed == {} and report.moved == [] and fake.writes() == []


def test_repair_changes_the_type_in_place_when_quickbooks_allows(tmp_path):
    dataset, fake, client = repair_fixture(tmp_path, [depreciation_journal(*j) for j in STAMPED])
    report = qbo.repair_account_types(dataset, client, write=True)
    assert report.fixed == {
        "Accumulated Depreciation": "Expense/Travel -> Fixed Asset/AccumulatedDepreciation in place"
    }
    account = fake.store["Account"][0]
    assert (account["Id"], account["AccountType"], account["AccountSubType"]) == (
        "900",
        "Fixed Asset",
        "AccumulatedDepreciation",
    )
    assert report.moved == [] and len(fake.writes()) == 1
    assert all(j["SyncToken"] == "0" for j in fake.store["JournalEntry"])  # untouched

    again = qbo.repair_account_types(dataset, client, write=True)
    assert (again.planned, again.fixed, again.refused) == ({}, {}, {})
    assert len(fake.writes()) == 1


def test_repair_replaces_the_account_when_quickbooks_refuses_the_type_change(tmp_path):
    dataset, fake, client = repair_fixture(
        tmp_path, [depreciation_journal(*j) for j in STAMPED], refuse_retype=True
    )
    report = qbo.repair_account_types(dataset, client, write=True)
    assert report.failed == {}
    assert "as new account" in report.fixed["Accumulated Depreciation"]
    assert "2 entries moved" in report.fixed["Accumulated Depreciation"]
    assert report.moved == ["qbo:JournalEntry:j1", "qbo:JournalEntry:j2"]

    old, new = fake.store["Account"][0], fake.store["Account"][-1]
    assert old["Name"] == "Accumulated Depreciation (wrong type, unused)"
    assert old["Active"] is False and old["AccountType"] == "Expense"
    assert (new["Name"], new["AccountType"], new["AccountSubType"]) == (
        "Accumulated Depreciation",
        "Fixed Asset",
        "AccumulatedDepreciation",
    )
    for journal, (_, ref) in zip(fake.store["JournalEntry"], STAMPED, strict=True):
        credit = journal["Line"][1]["JournalEntryLineDetail"]["AccountRef"]
        debit = journal["Line"][0]["JournalEntryLineDetail"]["AccountRef"]
        assert credit == {"value": new["Id"], "name": "Accumulated Depreciation"}
        assert debit == {"value": "901", "name": "Depreciation Expense"}
        assert qbo.loaded_ref(journal) == ref and journal["SyncToken"] == "1"
    # Each entry update was sparse and sent only Line: the stamp was never resent.
    sent = [json.loads(r.content) for r in fake.writes() if r.url.path.endswith("journalentry")]
    assert all(set(body) == {"Id", "SyncToken", "sparse", "Line"} for body in sent)

    # Mapping now gives the dataset's type; a second run has nothing to do.
    ledger = qbo.QboClient(client.config, client.http).fetch_ledger()
    assert {line.account_type for e in ledger for line in e.lines} == {"Expense", "Fixed Asset"}
    writes = len(fake.writes())
    again = qbo.repair_account_types(dataset, client, write=True)
    assert (again.planned, again.fixed, again.refused, again.failed) == ({}, {}, {}, {})
    assert len(fake.writes()) == writes


def test_repair_finishes_a_run_that_stopped_part_way(tmp_path):
    journals = [depreciation_journal("j1", STAMPED[0][1], account_id="902")]
    journals.append(depreciation_journal("j2", STAMPED[1][1], account_id="900"))
    dataset, fake, client = repair_fixture(tmp_path, journals)
    fake.store["Account"][0]["Name"] = "Accumulated Depreciation (wrong type, unused)"
    fake.store["Account"].append(
        {
            "Id": "902",
            "SyncToken": "0",
            "Name": "Accumulated Depreciation",
            "AccountType": "Fixed Asset",
        }
    )
    report = qbo.repair_account_types(dataset, client, write=True)
    assert report.fixed == {
        "Accumulated Depreciation": "finished: 1 entries moved to a Fixed Asset"
    }
    assert report.moved == ["qbo:JournalEntry:j2"]
    assert fake.store["Account"][0]["Active"] is False
    lines = [j["Line"][1]["JournalEntryLineDetail"]["AccountRef"]["value"] for j in journals]
    assert lines == ["902", "902"]


def test_repair_refuses_an_account_that_records_we_did_not_stamp_use(tmp_path):
    journals = [depreciation_journal(*j) for j in STAMPED]
    journals.append(depreciation_journal("j3", None))  # one of Intuit's own
    dataset, fake, client = repair_fixture(tmp_path, journals)
    report = qbo.repair_account_types(dataset, client, write=True)
    assert report.fixed == {} and report.moved == []
    assert "1 records we did not stamp use it" in report.refused["Accumulated Depreciation"]
    assert fake.writes() == []
    assert fake.store["Account"][0]["AccountType"] == "Expense"


def test_repair_refuses_production_before_any_request(tmp_path):
    fake = depreciation_sandbox([])
    client = make_client(fake, tmp_path, {**ENV, "QBO_ENVIRONMENT": "production"})
    with pytest.raises(qbo.QboConfigError, match="sandbox"):
        qbo.repair_account_types(tmp_path, client, write=True)
    assert fake.requests == []


def test_cli_repair_types_is_a_dry_run_without_write(tmp_path, monkeypatch, capsys):
    dataset, fake, client = repair_fixture(tmp_path, [depreciation_journal(*j) for j in STAMPED])
    monkeypatch.setattr(qbo.QboConfig, "from_env", classmethod(lambda cls: client.config))
    monkeypatch.setattr(qbo, "QboClient", lambda config: client)
    assert qbo.main(["repair-types", str(dataset)]) == 0
    assert capsys.readouterr().out.startswith("dry run: planned 1, fixed 0")
    assert fake.writes() == []
