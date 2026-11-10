"""Builds agent/examples/sample: a tiny hand-written dataset for developing the agent.

It is not the real test data and has nothing to do with the sealed answer key.
It exists so the agent can run before the real dataset does. Rebuild with:

    uv run python agent/examples/make_sample.py
"""

from datetime import date
from pathlib import Path

import auditorium_contracts as c

ROOT = Path(__file__).parent / "sample"

INVOICES = {
    # The pitch example: December work, invoiced and recorded in January, no accrual.
    "inv-2041": "Northwind Advisory LLC\nInvoice INV-2041\nInvoice date: January 8, 2026\n"
    "Bill to: Larkspur Labs, Inc.\nStrategy consulting\n"
    "Services rendered: December 1 - December 31, 2025\nTotal due: $12,000.00\n",
    # December work recorded in January, but accrued on December 31. Booked another way.
    "inv-883": "Harlow & Finch LLP\nInvoice 883\nInvoice date: January 10, 2026\n"
    "Bill to: Larkspur Labs, Inc.\nLegal services\n"
    "Service period: 12/01/2025 - 12/31/2025\nAmount due: $4,500.00\n",
    # Clean: November service recorded in November.
    "inv-5520": "Cloudline Hosting, Inc.\nInvoice 5520\nInvoice date: November 30, 2025\n"
    "Bill to: Larkspur Labs, Inc.\nCloud hosting for the month of November 2025\nTotal: $2,300.00\n",
    # Recorded just after year end and the invoice does not say when the work was done.
    "inv-17": "Brightside Design Co.\nInvoice 17\nInvoice date: January 5, 2026\n"
    "Bill to: Larkspur Labs, Inc.\nBrand design work\nTotal: $3,000.00\n",
    # Clean: January service recorded in January.
    "inv-5521": "Cloudline Hosting, Inc.\nInvoice 5521\nInvoice date: January 31, 2026\n"
    "Bill to: Larkspur Labs, Inc.\nCloud hosting for the month of January 2026\nTotal: $2,300.00\n",
    "rcpt-delta": "Delta Air Lines\nReceipt\nDate: October 3, 2025\nSFO to JFK\nTotal charged: $1,284.00\n",
}

# id, kind, vendor, cents, transaction date, invoice number, document, ledger date (None = not in the books)
TRANSACTIONS = [
    (
        "b_1001",
        "vendor_bill",
        "Northwind Advisory LLC",
        1_200_000,
        date(2026, 1, 8),
        "INV-2041",
        "inv-2041",
        date(2026, 1, 8),
    ),
    (
        "b_1002",
        "vendor_bill",
        "Harlow & Finch LLP",
        450_000,
        date(2026, 1, 10),
        "883",
        "inv-883",
        date(2026, 1, 10),
    ),
    (
        "b_1003",
        "vendor_bill",
        "Cloudline Hosting, Inc.",
        230_000,
        date(2025, 11, 30),
        "5520",
        "inv-5520",
        date(2025, 11, 30),
    ),
    (
        "b_1004",
        "vendor_bill",
        "Brightside Design Co.",
        300_000,
        date(2026, 1, 5),
        "17",
        "inv-17",
        date(2026, 1, 5),
    ),
    (
        "b_1005",
        "vendor_bill",
        "Cloudline Hosting, Inc.",
        230_000,
        date(2026, 1, 31),
        "5521",
        "inv-5521",
        date(2026, 1, 31),
    ),
    (
        "c_2001",
        "card_expense",
        "Delta Air Lines",
        128_400,
        date(2025, 10, 3),
        None,
        "rcpt-delta",
        date(2025, 10, 3),
    ),
]


def main():
    (ROOT / "documents").mkdir(parents=True, exist_ok=True)
    transactions, ledger, documents = [], [], []
    for number, (key, kind, vendor, cents, day, invoice, doc, booked) in enumerate(
        TRANSACTIONS, 200
    ):
        is_bill = kind == "vendor_bill"
        source = c.SourceRef(
            system="ramp", record_type="bill" if is_bill else "transaction", record_id=key
        )
        path = ROOT / "documents" / f"{doc}.txt"
        path.write_text(INVOICES[doc])
        documents.append(
            c.Document(
                id=f"doc_{doc}",
                type="invoice" if is_bill else "receipt",
                source=source,
                origin="external",
                path=f"documents/{doc}.txt",
                media_type="text/plain",
                sha256=c.sha256_file(path),
            )
        )
        transactions.append(
            c.SpendTransaction(
                id=f"ramp:{'bill' if is_bill else 'card'}:{key}",
                kind=kind,
                source=source,
                vendor_name=vendor,
                amount_cents=cents,
                transaction_date=day,
                invoice_number=invoice,
                spender_id="p_eng",
                document_ids=[f"doc_{doc}"],
            )
        )
        if booked:
            qbo_type = "Bill" if is_bill else "Purchase"
            ledger.append(
                c.LedgerEntry(
                    id=f"qbo:{qbo_type}:{number}",
                    source=c.SourceRef(system="qbo", record_type=qbo_type, record_id=str(number)),
                    entry_type="bill" if is_bill else "expense",
                    txn_date=booked,
                    # QuickBooks vendor names rarely match the card platform's exactly.
                    vendor_name=vendor.replace(", Inc.", "").replace(" LLC", ""),
                    doc_number=invoice,
                    total_cents=cents,
                )
            )
    ledger.append(
        c.LedgerEntry(
            id="qbo:JournalEntry:310",
            source=c.SourceRef(system="qbo", record_type="JournalEntry", record_id="310"),
            entry_type="journal_entry",
            txn_date=date(2025, 12, 31),
            memo="Accrue December legal fees - Harlow & Finch",
            total_cents=450_000,
            lines=[
                c.LedgerLine(
                    account="Legal & Professional Fees", side="debit", amount_cents=450_000
                ),
                c.LedgerLine(account="Accrued Liabilities", side="credit", amount_cents=450_000),
            ],
        )
    )

    c.write_json(
        ROOT / "manifest.json",
        c.DatasetManifest(
            dataset_id="agent-sample",
            company_name="Larkspur Labs, Inc.",
            contract_version=c.CONTRACT_VERSION,
            fiscal_year_start=date(2025, 1, 1),
            fiscal_year_end=date(2025, 12, 31),
            coverage_end=date(2026, 2, 28),
            description="Hand-written sample for agent development. Not the scored dataset.",
        ),
    )
    c.write_jsonl(ROOT / "transactions.jsonl", transactions)
    c.write_jsonl(ROOT / "ledger.jsonl", ledger)
    c.write_jsonl(ROOT / "documents.jsonl", documents)
    c.write_jsonl(
        ROOT / "people.jsonl",
        [
            c.Person(id="p_cfo", name="Dana Reyes", role="cfo"),
            c.Person(id="p_eng", name="Tom Alvarez", role="engineer", manager_id="p_cfo"),
        ],
    )
    c.write_json(
        ROOT / "policy.json",
        c.SpendPolicy(
            id="policy_2025",
            effective_from=date(2025, 1, 1),
            receipt_required_from_cents=7500,
            approval_rules=[],
        ),
    )
    problems = c.validate_dataset(ROOT)
    print("\n".join(problems) or f"OK  {ROOT}")


if __name__ == "__main__":
    main()
