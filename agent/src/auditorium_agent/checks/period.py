"""The period check: is the transaction recorded in the year the cost belongs to?

The question an auditor asks at year end is cut-off: did December's costs land
in December's books. So this check fails only when the books record a cost in a
later fiscal year than the one it was incurred in, and nothing in the earlier
year (an accrual) already booked it. Reasons are in agent/decisions/.
"""

from __future__ import annotations

from datetime import date, timedelta

from auditorium_contracts import (
    CheckName,
    CheckResult,
    CheckStatus,
    DocumentEvidence,
    Evidence,
    IssueCode,
    RecordEvidence,
    SourceSystem,
    SpendTransaction,
    TransactionKind,
    period_of,
    result_id,
)

from ..books import Books
from ..evidence import record_evidence
from ..matching import VENDOR_UNCLEAR, accrual_size, accrued_amount_field, find_accruals
from ..service_period import find_service_period


def _money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _day(day: date) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def _month(day: date) -> str:
    return day.strftime("%B %Y")


def _delivered(
    txn: SpendTransaction, books: Books, ask_model: bool = False
) -> tuple[date, date, Evidence, bool]:
    """When the cost was incurred: start, end, the evidence, and whether a document states it.

    With `ask_model`, a document the patterns cannot read is passed to the model reader.
    """
    if txn.kind is TransactionKind.CARD_EXPENSE:
        day = txn.transaction_date
        evidence = RecordEvidence(
            record_type="transaction",
            record_id=txn.id,
            field="transaction_date",
            value=day.isoformat(),
            note="The purchase date.",
        )
        return day, day, evidence, True
    for document_id in txn.document_ids:
        if document_id not in books.documents:
            continue
        pages = books.read_document(document_id)
        stated = find_service_period(pages)
        if stated is None and ask_model:
            stated = books.ask_service_period(document_id, pages)
        if stated:
            note = "When the work was done, as the invoice states it."
            if stated.read_by:
                note = (
                    f"When the work was done. {stated.read_by} read the dates "
                    f"{stated.start} to {stated.end} from this line. The line is checked "
                    "against the document; the dates are the model's reading of it."
                )
            evidence = DocumentEvidence(
                document_id=document_id,
                page=stated.page,
                quote=stated.quote,
                note=note,
            )
            return stated.start, stated.end, evidence, True
    day = txn.transaction_date
    evidence = RecordEvidence(
        record_type="transaction",
        record_id=txn.id,
        field="transaction_date",
        value=day.isoformat(),
        note="The invoice date. No attached document states when the work was done.",
    )
    return day, day, evidence, False


def check_period(txn: SpendTransaction, books: Books, run_id: str) -> CheckResult:
    def result(status, summary, rationale, evidence, *, failed=False, recorded=None, expected=None):
        return CheckResult(
            id=result_id(txn.id, CheckName.PERIOD),
            run_id=run_id,
            transaction_id=txn.id,
            check=CheckName.PERIOD,
            status=status,
            issue_codes=[IssueCode.PERIOD_WRONG] if failed else [],
            summary=summary,
            rationale=rationale,
            evidence=evidence,
            recorded_period=period_of(recorded) if recorded else None,
            expected_period=period_of(expected) if expected else None,
        )

    match = books.matches[txn.id]
    entry = None if match.how == VENDOR_UNCLEAR else match.entry
    if entry is None:
        search = books.search(
            SourceSystem.QBO,
            f"bill or expense entries for vendor '{txn.vendor_name}' totalling {_money(txn.amount_cents)}",
            [],
        )
        return result(
            CheckStatus.NOT_APPLICABLE,
            "Not found in the books, so there is no recorded period to test.",
            "The period check compares when a cost was incurred with when the books recorded "
            "it. No ledger entry matches this transaction. The ledger_match check reports that.",
            [search],
        )

    recorded = entry.txn_date
    recorded_evidence = RecordEvidence(
        record_type="ledger_entry",
        record_id=entry.id,
        field="txn_date",
        value=recorded.isoformat(),
        note="The date the books recorded it.",
    )
    start, end, delivered_evidence, stated = _delivered(txn, books)
    if not stated and books.side(recorded) == books.side(end) == 1:
        # Recorded just after year end with no dates the patterns can find. This is the one
        # case that turns on when the work was done, so it is the one case worth a model call.
        start, end, delivered_evidence, stated = _delivered(txn, books, ask_model=True)
    evidence = [delivered_evidence, recorded_evidence]
    periods = {"recorded": recorded, "expected": end}
    recorded_side, end_side = books.side(recorded), books.side(end)

    if books.side(start) != end_side:
        return result(
            CheckStatus.NEEDS_REVIEW,
            f"The work ran from {_day(start)} to {_day(end)}, across the fiscal year end.",
            "Part of this cost belongs to each fiscal year. How much is a judgement for a person.",
            evidence,
            **periods,
        )
    if not stated and recorded_side == end_side == 1:
        return result(
            CheckStatus.NEEDS_REVIEW,
            f"Recorded in {_month(recorded)}, just after year end, and the invoice does not say "
            "when the work was done.",
            "If the work was done before year end, this belongs in the earlier year. The invoice "
            "date alone cannot settle that.",
            evidence,
            **periods,
        )
    if recorded_side == end_side:
        return result(
            CheckStatus.PASS,
            f"Incurred in {_month(end)} and recorded in {_month(recorded)}, the same fiscal year.",
            "The cost is recorded in the fiscal year it was incurred in.",
            evidence,
            **periods,
        )
    if recorded_side < end_side:
        return result(
            CheckStatus.NEEDS_REVIEW,
            f"Recorded in {_month(recorded)}, before the work was done in {_month(end)}.",
            "This may be a prepayment that belongs in the later year. A person should confirm "
            "how it was treated.",
            evidence,
            **periods,
        )

    # Recorded in a later fiscal year than it was incurred. Did the earlier year book it another way?
    window_start = end.replace(day=1)
    window_end = (
        books.fiscal_year_end if end_side == 0 else books.fiscal_year_start - timedelta(days=1)
    )
    accruals = find_accruals(txn, books.ledger, window_start, window_end)
    search = books.search(
        SourceSystem.QBO,
        f"journal entries dated {window_start} to {window_end} naming vendor '{txn.vendor_name}'",
        [accrual.id for accrual in accruals],
    )
    evidence.append(search)
    exact = [
        (accrual, field)
        for accrual in accruals
        if (field := accrued_amount_field(accrual, txn.amount_cents))
    ]
    if exact:
        accrual, field = exact[0]
        evidence.append(
            record_evidence(
                "ledger_entry",
                accrual,
                field,
                f"Accrual dated {accrual.txn_date} for the same amount.",
            )
        )
        exact = [accrual]
        return result(
            CheckStatus.PASS,
            f"Work done in {_month(end)} was billed in {_month(recorded)}, and an accrual on "
            f"{_day(exact[0].txn_date)} put the cost in the right year.",
            "The bill is recorded after year end, but a journal entry in the year the work was "
            "done already booked the same amount for this vendor.",
            evidence,
            **periods,
        )
    if accruals:
        return result(
            CheckStatus.NEEDS_REVIEW,
            f"Work done in {_month(end)} is recorded in {_month(recorded)}. An accrual for this "
            "vendor exists but for a different amount.",
            f"The bill is {_money(txn.amount_cents)}. The accrual found is "
            f"{_money(accrual_size(accruals[0]))}. A person should confirm it covers this bill.",
            evidence,
            **periods,
        )
    return result(
        CheckStatus.FAIL,
        f"Work done in {_month(end)} is first recorded in {_month(recorded)}, with no accrual "
        "in the year it was done. It is recorded in the wrong year.",
        f"The cost of {_money(txn.amount_cents)} was incurred by {_day(end)}. The only ledger "
        f"entry for it is dated {_day(recorded)}, in the next fiscal year. A search of journal "
        "entries in the earlier year found none naming this vendor, so nothing booked it there.",
        evidence,
        failed=True,
        **periods,
    )
