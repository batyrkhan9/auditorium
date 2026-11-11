"""Builds agent/examples/sample: a tiny hand-written dataset for developing the agent.

It is not the real test data and has nothing to do with the sealed answer key.
It exists so the agent can run before the real dataset does. Rebuild with:

    uv run python agent/examples/make_sample.py
"""

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import auditorium_contracts as c
from fpdf import FPDF

ROOT = Path(__file__).parent / "sample"
BILL_TO = "Bill to: Larkspur Labs, Inc.\n"

# Each case: the Ramp side, the invoice or receipt text, and how the books recorded it.
# `booked` is the ledger date (None = not in the books). `booked_cents` overrides the
# ledger amount. `copies` is how many ledger entries exist. `approvals` is who approved
# it and when. `paid` is the day a bill was paid. Everyone's spender is Tom (p_eng).
# Policy: card charges from $500 need the spender's manager; bills from $5,000 need the CFO.
CASES = [
    dict(  # The pitch example: December work, invoiced and recorded in January, no accrual.
        key="b_1001", vendor="Northwind Advisory LLC", cents=1_200_000, day=date(2026, 1, 8),
        invoice="INV-2041", booked=date(2026, 1, 8),
        approvals=[("p_cfo", date(2026, 1, 9))], paid=date(2026, 1, 22),
        text="Northwind Advisory LLC\nInvoice INV-2041\nInvoice date: January 8, 2026\n" + BILL_TO
        + "Strategy consulting\nServices rendered: December 1 - December 31, 2025\nTotal due: $12,000.00\n",
    ),
    dict(  # December work recorded in January, but accrued on December 31.
        key="b_1002", vendor="Harlow & Finch LLP", cents=450_000, day=date(2026, 1, 10),
        invoice="883", booked=date(2026, 1, 10), paid=date(2026, 1, 24),
        text="Harlow & Finch LLP\nInvoice 883\nInvoice date: January 10, 2026\n" + BILL_TO
        + "Legal services for the Series B financing and related corporate housekeeping. Service period: "
        "December 1 - December 31, 2025\nAmount due: $4,500.00\n",
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
        booked=date(2025, 10, 3), approvals=[("p_mgr", date(2025, 10, 4))],
        text="Delta Air Lines\nReceipt\nDate: October 3, 2025\nSFO to JFK\nTotal charged: $1,284.00\n",
    ),
    dict(  # Card charge that never reached the books.
        key="c_2002", vendor="Figma", cents=54_000, day=date(2025, 9, 12), booked=None,
        approvals=[("p_mgr", date(2025, 9, 13))],
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
        invoice="M-204", booked=date(2025, 4, 9), approvals=[("p_cfo", date(2025, 4, 10))],
        text="Kestrel Print Shop\nInvoice 5512\nInvoice date: March 30, 2025\n" + BILL_TO
        + "Conference banners\nTotal: $310.00\n",
    ),
    dict(  # Over the bill threshold and nobody approved it.
        key="b_1009", vendor="Quillon Security", cents=820_000, day=date(2025, 5, 20),
        invoice="QS-88", booked=date(2025, 5, 20),
        text="Quillon Security\nInvoice QS-88\nInvoice date: May 20, 2025\n" + BILL_TO
        + "Penetration test\nTotal: $8,200.00\n",
    ),
    dict(  # Approved, but by an engineering manager where the rule requires the CFO.
        key="b_1010", vendor="Redwood Recruiting", cents=750_000, day=date(2025, 6, 3),
        invoice="RR-410", booked=date(2025, 6, 3), approvals=[("p_mgr", date(2025, 6, 4))],
        text="Redwood Recruiting\nInvoice RR-410\nInvoice date: June 3, 2025\n" + BILL_TO
        + "Placement fee\nTotal: $7,500.00\n",
    ),
    dict(  # The CFO approved it four days after it was paid.
        key="b_1011", vendor="Sable Freight", cents=900_000, day=date(2025, 3, 3),
        invoice="SF-2207", booked=date(2025, 3, 3), paid=date(2025, 3, 10),
        approvals=[("p_cfo", date(2025, 3, 14))],
        text="Sable Freight\nInvoice SF-2207\nInvoice date: March 3, 2025\n" + BILL_TO
        + "Equipment shipping\nTotal: $9,000.00\n",
    ),
    dict(  # The spender approved their own card charge.
        key="c_2006", vendor="Apple Store", cents=115_000, day=date(2025, 2, 11),
        booked=date(2025, 2, 11), approvals=[("p_eng", date(2025, 2, 11))],
        text="Apple Store\nReceipt\nDate: February 11, 2025\nDisplay adapter and monitor\n"
        "Total: $1,150.00\n",
    ),
    dict(  # December work billed in January, said in words no pattern catches. No accrual.
        key="b_1012", vendor="Ironwood Design Studio", cents=360_000, day=date(2026, 1, 6),
        invoice="2291", booked=date(2026, 1, 6),
        text="Ironwood Design Studio\nInvoice 2291\nInvoice date: January 6, 2026\n" + BILL_TO
        + "This invoice covers the design sprint we ran for you in the last two weeks of December 2025.\n"
        "Total: $3,600.00\n",
    ),
    dict(  # The card statement codes the merchant; the receipt spells it out.
        key="c_2007", vendor="SQ *BLUEBTL OAK", cents=8_640, day=date(2025, 9, 3),
        booked=date(2025, 9, 3),
        text="Blue Bottle Coffee\nOakland, Broadway\nSeptember 3, 2025\n"
        "Team offsite coffee and pastries\nTotal: $86.40\n",
    ),
]  # fmt: skip

BANK_STATEMENTS = {
    "2025-03": "First Harbor Bank\nStatement for March 2025\nLarkspur Labs, Inc. Operating account\n"
    "03/10/2025  ACH debit  SABLE FREIGHT  9,000.00\n"
    "03/28/2025  ACH debit  GUSTO PAYROLL  48,210.55\n",
    # The Harlow & Finch bill was paid in January too, and is not on this statement.
    "2026-01": "First Harbor Bank\nStatement for January 2026\nLarkspur Labs, Inc. Operating account\n"
    "01/22/2026  ACH debit  NORTHWIND ADVISORY  12,000.00\n"
    "01/30/2026  ACH debit  GUSTO PAYROLL  51,904.10\n",
}

# What an auditor would send: lines copied from the ledger, with no internal ids.
SAMPLE_REQUEST = c.SampleRequest(
    id="sample-1",
    auditor="Hale & Whitlock LLP",
    received_on=date(2026, 2, 16),
    items=[
        c.SampleItem(line=1, description="Northwind Advisory, consulting", vendor_name="Northwind Advisory", amount_cents=1_200_000, transaction_date=date(2026, 1, 8), reference="INV-2041"),
        c.SampleItem(line=2, description="Hotel Kabuki", vendor_name="Hotel Kabuki", amount_cents=31_240, transaction_date=date(2025, 7, 22)),
        c.SampleItem(line=3, description="Quillon Security, pen test", vendor_name="Quillon Security", amount_cents=820_000, reference="QS-88"),
        c.SampleItem(line=4, description="Meridian Analytics", vendor_name="Meridian Analytics", amount_cents=640_000),
        # The auditor copied the ledger's amount, which is not what Ramp has.
        c.SampleItem(line=5, description="Pinecrest Staffing", vendor_name="Pinecrest Staffing", amount_cents=158_000, transaction_date=date(2025, 10, 20)),
        c.SampleItem(line=6, description="Harlow & Finch, legal", vendor_name="Harlow & Finch", amount_cents=450_000, reference="883"),
        # Two monthly bills fit this line.
        c.SampleItem(line=7, description="Cloudline Hosting", vendor_name="Cloudline Hosting", amount_cents=230_000),
        c.SampleItem(line=8, description="Zephyr Catering, offsite lunch", vendor_name="Zephyr Catering", amount_cents=41_500, transaction_date=date(2025, 8, 2)),
    ],
)  # fmt: skip


def write_pdf(path: Path, text: str) -> Path:
    """Lay the text out the way real invoices and statements are: a heading, amounts
    right-aligned in their own column, statement rows as a table, long lines wrapped."""
    pdf = FPDF(format="Letter")
    pdf.set_creation_date(datetime(2026, 1, 1, tzinfo=UTC))  # so rebuilding gives the same bytes
    pdf.add_page()
    heading, *lines = text.splitlines()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, heading, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=11)
    for line in lines:
        label, colon, value = line.partition(": ")
        columns = re.split(r"\s{2,}", line)
        if colon and re.fullmatch(r"\$[\d,]+\.\d{2}", value):
            pdf.cell(130, 7, f"{label}:")
            pdf.cell(0, 7, value, align="R", new_x="LMARGIN", new_y="NEXT")
        elif len(columns) > 2:
            for column, width in zip(columns[:-1], (28, 32, 80), strict=False):
                pdf.cell(width, 7, column)
            pdf.cell(0, 7, columns[-1], align="R", new_x="LMARGIN", new_y="NEXT")
        else:
            pdf.multi_cell(0, 7, line, new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))
    return path


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
            path = write_pdf(ROOT / "documents" / f"{key}.pdf", case["text"])
            documents.append(
                c.Document(
                    id=f"doc_{key}",
                    type="invoice" if is_bill else "receipt",
                    source=source,
                    origin="external",
                    path=f"documents/{key}.pdf",
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
                paid_date=case.get("paid"),
                approvals=[
                    c.Approval(
                        approver_id=person,
                        decision="approved",
                        decided_at=datetime(day.year, day.month, day.day, 15, tzinfo=UTC),
                    )
                    for person, day in case.get("approvals", [])
                ],
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

    for month, text in BANK_STATEMENTS.items():
        path = write_pdf(ROOT / "documents" / f"bank-{month}.pdf", text)
        documents.append(
            c.Document(
                id=f"doc_bank_{month}",
                type="bank_statement",
                source=c.SourceRef(system="bank", record_type="statement", record_id=month),
                origin="external",
                path=f"documents/bank-{month}.pdf",
                sha256=c.sha256_file(path),
            )
        )
    c.write_json(ROOT / "samples" / f"{SAMPLE_REQUEST.id}.json", SAMPLE_REQUEST)

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
            c.Person(id="p_ceo", name="Maya Okafor", role="ceo", email="maya@larkspur.example"),
            c.Person(
                id="p_cfo",
                name="Dana Reyes",
                role="cfo",
                email="dana@larkspur.example",
                manager_id="p_ceo",
            ),
            c.Person(
                id="p_mgr",
                name="Priya Nair",
                role="engineering manager",
                email="priya@larkspur.example",
                manager_id="p_ceo",
            ),
            c.Person(
                id="p_eng",
                name="Tom Alvarez",
                role="engineer",
                email="tom@larkspur.example",
                manager_id="p_mgr",
            ),
        ],  # fmt: skip
    )
    c.write_json(
        ROOT / "policy.json",
        c.SpendPolicy(
            id="policy_2025",
            effective_from=date(2025, 1, 1),
            receipt_required_from_cents=7500,
            approval_rules=[
                c.ApprovalRule(
                    id="card_500",
                    description="Card charges of $500 or more need the spender's manager.",
                    kinds=["card_expense"],
                    min_amount_cents=50_000,
                    approver_role="manager",
                ),
                c.ApprovalRule(
                    id="bill_5000",
                    description="Vendor bills of $5,000 or more need the CFO.",
                    kinds=["vendor_bill"],
                    min_amount_cents=500_000,
                    approver_role="cfo",
                ),
            ],
        ),
    )
    problems = c.validate_dataset(ROOT)
    print("\n".join(problems) or f"OK  {ROOT}")


if __name__ == "__main__":
    main()
