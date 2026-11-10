"""Draft the messages that ask coworkers for what is missing. Drafts only: nothing is sent.

A draft is written only for something a coworker can actually supply: a
receipt, the right invoice, or proof of an approval. Plain templates for now;
a model can tune the wording later without changing what is asked.
"""

from __future__ import annotations

from auditorium_contracts import CheckResult, FollowUpDraft, IssueCode, Person, SpendTransaction

from .books import Books
from .checks.approval import required_approvers, rules_that_apply


def _money(cents: int) -> str:
    return f"${abs(cents) / 100:,.2f}"


def _day(day) -> str:
    return f"{day:%B} {day.day}, {day.year}"


def _first_name(person: Person) -> str:
    return person.name.split()[0]


def _address(person: Person) -> str:
    return person.email or person.name


def drafts_for(
    txn: SpendTransaction, results: list[CheckResult], books: Books, run_id: str
) -> list[FollowUpDraft]:
    """One draft per missing thing a coworker can supply, addressed to the person who can."""
    issues = {code: result for result in results for code in result.issue_codes}
    spender = books.people.get(txn.spender_id or "")
    amount, vendor, day = _money(txn.amount_cents), txn.vendor_name, _day(txn.transaction_date)
    why = "Our auditors picked this transaction to test and want to see it."
    is_bill = txn.kind.value == "vendor_bill"
    thing = "invoice" if is_bill else "receipt"
    drafts = []

    if IssueCode.RECEIPT_MISSING in issues and spender:
        drafts.append(
            FollowUpDraft(
                id=f"draft:{txn.id}:receipt",
                run_id=run_id,
                channel="email" if is_bill else "slack",
                to=_address(spender),
                subject=f"{thing.capitalize()} needed: {vendor}, {amount}" if is_bill else None,
                body=f"Hi {_first_name(spender)}, could you send me the {thing} for the {amount} "
                f"{'bill from' if is_bill else 'charge at'} {vendor} on {day}? {why} "
                "If you no longer have it, tell me and I'll ask the vendor for a copy.",
                asks_for=f"{thing} for {amount} at {vendor} on {day}",
                result_ids=[issues[IssueCode.RECEIPT_MISSING].id],
            )
        )
    if IssueCode.RECEIPT_MISMATCH in issues and spender:
        drafts.append(
            FollowUpDraft(
                id=f"draft:{txn.id}:receipt",
                run_id=run_id,
                channel="email",
                to=_address(spender),
                subject=f"Wrong document attached: {vendor}, {amount}",
                body=f"Hi {_first_name(spender)}, the document attached to the {amount} "
                f"{'bill from' if is_bill else 'charge at'} {vendor} dated {day} is for "
                f"something else. Could you send me the {thing} for this one? {why}",
                asks_for=f"the correct {thing} for {amount} at {vendor} on {day}",
                result_ids=[issues[IssueCode.RECEIPT_MISMATCH].id],
            )
        )
    if IssueCode.APPROVAL_MISSING in issues:
        approvers = [
            person
            for _, rule in rules_that_apply(txn, books)
            for person in required_approvers(txn, rule, books)
        ]
        for person in dict.fromkeys(approvers):
            drafts.append(
                FollowUpDraft(
                    id=f"draft:{txn.id}:approval:{person.id}",
                    run_id=run_id,
                    channel="slack",
                    to=_address(person),
                    body=f"Hi {_first_name(person)}, our policy needs your approval on the {amount} "
                    f"{'bill from' if is_bill else 'charge at'} {vendor} "
                    f"dated {day}, and Ramp shows none. {why} If you approved it somewhere "
                    "else, an email or a thread, could you forward that to me? If you did "
                    "not, tell me, and we will say so to the auditors.",
                    asks_for=f"evidence of approval for {amount} at {vendor} on {day}",
                    result_ids=[issues[IssueCode.APPROVAL_MISSING].id],
                )
            )
    return drafts
