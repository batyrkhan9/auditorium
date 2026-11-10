"""Builds agent/examples/sample: a tiny hand-written dataset for developing the agent.

It is not the real test data and has nothing to do with the sealed answer key.
It exists so the agent can run before the real dataset does. Rebuild with:

    uv run python agent/examples/make_sample.py
"""

from datetime import date, timedelta
from pathlib import Path

import auditorium_contracts as c

ROOT = Path(__file__).parent / "sample"
BILL_TO = "Bill to: Larkspur Labs, Inc.\n"

# Each case: the Ramp side, the invoice or receipt text, and how the books recorded it.
# `booked` is the ledger date (None = not in the books). `booked_cents` overrides the
# ledger amount. `copies` is how many ledger entries exist.
CASES = [
    dict(  # The pitch example: December work, invoiced and recorded in January, no accrual.
        key="b_1001", vendor="Northwind Advisory LLC", cents=1_200_000, day=date(2026, 1, 8),
        invoice="INV-2041", booked=date(2026, 1, 8),
        text="Northwind Advisory LLC\nInvoice INV-2041\nInvoice date: January 8, 2026\n" + BILL_TO
        + "Strategy consulting\nServices rendered: December 1 - December 31, 2025\nTotal due: $12,000.00\n",
    ),
    dict(  # December work recorded in January, but accrued on December 31.
        key="b_1002", vendor="Harlow & Finch LLP", cents=450_000, day=date(2026, 1, 10),
        invoice="883", booked=date(2026, 1, 10),
        text="Harlow & Finch LLP\nInvoice 883\nInvoice date: January 10, 2026\n" + BILL_TO
        + "Legal services\nService period: 12/01/2025 - 12/31/2025\nAmount due: $4,500.00\n",
    ),
    dict(  # Clean. Same vendor and amount as b_1005: the pairing must keep them apart.
        key="b_1003", vendor="Cloudline Hosting, Inc.", cents=230_000, day=date(2025, 11, 30),
        invoice="5520", booked=date(2025, 11, 30),
        text="Cloudline Hosting, Inc.\nInvoice 5520\nInvoice date: November 30, 2025\n" + BILL_TO
        + "Cloud hosting for the month of November 2025\nTotal: $2,300.00\n",
    ),
    dict(  # Recorded just after year end; the invoice does not say when the work was done.
        key="b_1004", vendor="Brightside Design Co.", cents=300_000, day=date(2026, 1, 5),
        invoice="17", booked=date(2026, 1, 5),
        text="Brightside Design Co.\nInvoice 17\nInvoice date: January 5, 2026\n" + BILL_TO
        + "Brand design work\nTotal: $3,000.00\n",
    ),
    dict(  # Clean: January service recorded in January.
        key="b_1005", vendor="Cloudline Hosting, Inc.", cents=230_000, day=date(2026, 1, 31),
        invoice="5521", booked=date(2026, 1, 31),
        text="Cloudline Hosting, Inc.\nInvoice 5521\nInvoice date: January 31, 2026\n" + BILL_TO
        + "Cloud hosting for the month of January 2026\nTotal: $2,300.00\n",
    ),
    dict(  # The books have $1,580.00 where the bill is $1,850.00.
        key="b_1006", vendor="Pinecrest Staffing LLC", cents=185_000, day=date(2025, 10, 16),
        invoice="PS-301", booked=date(2025, 10, 20), booked_cents=158_000,
        text="Pinecrest Staffing LLC\nInvoice PS-301\nInvoice date: October 16, 2025\n" + BILL_TO
        + "Contract staffing\nServices rendered: October 1 - October 15, 2025\nTotal: $1,850.00\n",
    ),
    dict(  # Entered in the books twice.
        key="b_1007", vendor="Atlas Office Supply", cents=92_000, day=date(2025, 8, 14),
        invoice="A-77", booked=date(2025, 8, 14), copies=2,
        text="Atlas Office Supply\nInvoice A-77\nInvoice date: August 14, 2025\n" + BILL_TO
        + "Desks and chairs\nTotal: $920.00\n",
    ),
    dict(  # Clean card charge.
        key="c_2001", vendor="Delta Air Lines", cents=128_400, day=date(2025, 10, 3),
        booked=date(2025, 10, 3),
        text="Delta Air Lines\nReceipt\nDate: October 3, 2025\nSFO to JFK\nTotal charged: $1,284.00\n",
    ),
    dict(  # Card charge that never reached the books.
        key="c_2002", vendor="Figma", cents=54_000, day=date(2025, 9, 12), booked=None,
        text="Figma, Inc.\nReceipt\nDate: September 12, 2025\nProfessional plan\nTotal: $540.00\n",
    ),
    dict(  # Over the receipt threshold with nothing attached.
        key="c_2003", vendor="Hotel Kabuki", cents=31_240, day=date(2025, 7, 22),
        booked=date(2025, 7, 22), text=None,
    ),
    dict(  # Under the receipt threshold, so no receipt is needed.
        key="c_2004", vendor="Blue Bottle Coffee", cents=1_875, day=date(2025, 5, 6),
        booked=date(2025, 5, 6), text=None,
    ),
    dict(  # The receipt shows the meal; the card was charged the meal plus a tip.
        key="c_2005", vendor="Trattoria Lucca", cents=23_000, day=date(2025, 6, 18),
        booked=date(2025, 6, 18),
        text="Trattoria Lucca\nDate: June 18, 2025\nTable 12, 4 guests\nSubtotal: $183.50\n"
        "Tax: $16.50\nTotal: $200.00\n",
    ),
    dict(  # Somebody attached another vendor's invoice.
        key="b_1008", vendor="Meridian Analytics", cents=640_000, day=date(2025, 4, 9),
        invoice="M-204", booked=date(2025, 4, 9),
        text="Kestrel Print Shop\nInvoice 5512\nInvoice date: March 30, 2025\n" + BILL_TO
        + "Conference banners\nTotal: $310.00\n",
    ),
]  # fmt: skip


def main():
    (ROOT / "documents").mkdir(parents=True, exist_ok=True)
    transactions, ledger, documents = [], [], []
    for number, case in enumerate(CASES, 200):
        key, vendor = case["key"], case["vendor"]
        is_bill = key.startswith("b_")
        source = c.SourceRef(
            system="ramp", record_type="bill" if is_bill else "transaction", record_id=key
        )
        if case["text"]:
            path = ROOT / "documents" / f"{key}.txt"
            path.write_text(case["text"])
            documents.append(
                c.Document(
                    id=f"doc_{key}",
                    type="invoice" if is_bill else "receipt",
                    source=source,
                    origin="external",
                    path=f"documents/{key}.txt",
                    media_type="text/plain",
                    sha256=c.sha256_file(path),
                )
            )
        transactions.append(
            c.SpendTransaction(
                id=f"ramp:{'bill' if is_bill else 'card'}:{key}",
                kind="vendor_bill" if is_bill else "card_expense",
                source=source,
                vendor_name=vendor,
                amount_cents=case["cents"],
                transaction_date=case["day"],
                invoice_number=case.get("invoice"),
                spender_id="p_eng",
                document_ids=[f"doc_{key}"] if case["text"] else [],
            )
        )
        qbo_type = "Bill" if is_bill else "Purchase"
        for copy in range(case.get("copies", 1) if case["booked"] else 0):
            record_id = str(number + 500 * copy)
            ledger.append(
                c.LedgerEntry(
                    id=f"qbo:{qbo_type}:{record_id}",
                    source=c.SourceRef(system="qbo", record_type=qbo_type, record_id=record_id),
                    entry_type="bill" if is_bill else "expense",
                    txn_date=case["booked"] + timedelta(days=copy),
                    # QuickBooks vendor names rarely match the card platform's exactly.
                    vendor_name=vendor.replace(", Inc.", "").replace(" LLC", ""),
                    doc_number=case.get("invoice"),
                    total_cents=case.get("booked_cents", case["cents"]),
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
