"""The approval check: did a rule require approval, and did the right person give it in time?

The rules come from the company's own policy, not from this code. A rule names
who must approve: the spender's manager, or whoever holds a role. Reasons for
where the lines are drawn are in agent/decisions/.
"""

from __future__ import annotations

from auditorium_contracts import (
    ApprovalRule,
    CheckName,
    CheckResult,
    CheckStatus,
    IssueCode,
    Person,
    SpendTransaction,
    TransactionKind,
    result_id,
)

from ..books import Books
from ..evidence import record_evidence

# Worst outcome first: when several rules apply, every one of them must be met.
_RANK = {CheckStatus.FAIL: 0, CheckStatus.NEEDS_REVIEW: 1, CheckStatus.PASS: 2}


def _money(cents: int) -> str:
    return f"${abs(cents) / 100:,.2f}"


def _day(moment) -> str:
    return f"{moment:%B} {moment.day}, {moment.year}"


def rules_that_apply(txn: SpendTransaction, books: Books) -> list[tuple[int, ApprovalRule]]:
    """The policy's approval rules that cover this transaction, with their position in the policy."""
    return [
        (index, rule)
        for index, rule in enumerate(books.policy.approval_rules)
        if txn.kind in rule.kinds and abs(txn.amount_cents) >= rule.min_amount_cents
    ]


def required_approvers(txn: SpendTransaction, rule: ApprovalRule, books: Books) -> list[Person]:
    """The people this rule names as approver for this transaction. Empty if the records do not say."""
    people = books.people
    if rule.approver_role == "manager":
        spender = people.get(txn.spender_id or "")
        manager = people.get(spender.manager_id or "") if spender else None
        return [manager] if manager else []
    return [p for p in people.values() if (p.role or "").lower() == rule.approver_role.lower()]


def _judge(txn: SpendTransaction, index: int, rule: ApprovalRule, books: Books, what: str):
    """Test one rule that applies: status, issue, summary, rationale, evidence."""
    policy, people = books.policy, books.people
    at = f"approval_rules.{index}"
    evidence = [
        record_evidence(
            "policy", policy, f"{at}.min_amount_cents", "The rule applies from this amount."
        ),
        record_evidence("policy", policy, f"{at}.approver_role", "Who the rule says must approve."),
    ]
    spender = people.get(txn.spender_id or "")
    holders = required_approvers(txn, rule, books)

    if rule.approver_role == "manager":
        manager = holders[0] if holders else None
        if manager is None:
            if txn.spender_id:
                evidence.append(record_evidence("transaction", txn, "spender_id", "The spender."))
            return (
                CheckStatus.NEEDS_REVIEW,
                None,
                "The rule requires the spender's manager, and the records do not say who that is.",
                "Without the spender or their manager on record, the approver cannot be judged.",
                evidence,
            )
        required = {manager.id}
        who = f"{manager.name}, {spender.name}'s manager"
        evidence.append(
            record_evidence("person", spender, "manager_id", f"{spender.name}'s manager.")
        )
    else:
        if not holders:
            return (
                CheckStatus.NEEDS_REVIEW,
                None,
                f"The rule requires approval by the {rule.approver_role}, and nobody on record "
                "holds that role.",
                "The approver cannot be judged without knowing who holds the role.",
                evidence,
            )
        required = {p.id for p in holders}
        who = f"{' or '.join(p.name for p in holders)} ({rule.approver_role})"
        evidence += [record_evidence("person", p, "role", f"{p.name}'s role.") for p in holders]

    if txn.approvals is None:
        # Not the same as an empty list: the source could not tell us either way.
        evidence.append(
            record_evidence("transaction", txn, "approvals", "The source gave no approval data.")
        )
        return (
            CheckStatus.NEEDS_REVIEW,
            None,
            f"This {_money(txn.amount_cents)} {what} needs approval by {who}, and the source "
            "could not say whether it was approved.",
            "No approval data came back for this transaction. That is not evidence that "
            "approval is missing, so a person should look it up.",
            evidence,
        )
    approved = [(n, a) for n, a in enumerate(txn.approvals) if a.decision == "approved"]
    if not approved:
        evidence.append(
            record_evidence("transaction", txn, "approvals", "The approvals recorded in Ramp.")
        )
        if any(a.decision == "pending" for a in txn.approvals):
            return (
                CheckStatus.FAIL,
                IssueCode.APPROVAL_MISSING,
                f"This {_money(txn.amount_cents)} {what} needs approval by {who}. The request "
                "is still pending and nobody has approved it.",
                "The rule applies at this amount. An approval was requested and has not been "
                "given, so the spend went ahead unapproved.",
                evidence,
            )
        return (
            CheckStatus.FAIL,
            IssueCode.APPROVAL_MISSING,
            f"This {_money(txn.amount_cents)} {what} needs approval by {who} and has none.",
            "The rule applies at this amount and Ramp records no approval.",
            evidence,
        )

    valid = [
        (n, a) for n, a in approved if a.approver_id in required and a.approver_id != txn.spender_id
    ]
    if not valid:
        n, approval = approved[0]
        approver = people.get(approval.approver_id)
        name = approver.name if approver else approval.approver_id
        evidence.append(
            record_evidence("transaction", txn, f"approvals.{n}.approver_id", "Who approved it.")
        )
        if approval.approver_id == txn.spender_id:
            evidence.append(
                record_evidence("transaction", txn, "spender_id", "Who spent the money.")
            )
            summary = f"{name} approved their own {what}. The rule requires {who}."
            rationale = "The person who spent the money cannot be the one who approves it."
        else:
            if approver and approver.role:
                evidence.append(record_evidence("person", approver, "role", f"{name}'s role."))
            role = f" ({approver.role})" if approver and approver.role else ""
            summary = f"Approved by {name}{role}, but the rule requires {who}."
            rationale = "Someone approved it, but not the person the rule names."
        return CheckStatus.FAIL, IssueCode.APPROVAL_WRONG_APPROVER, summary, rationale, evidence

    n, approval = min(valid, key=lambda pair: pair[1].decided_at)
    name = people[approval.approver_id].name
    evidence += [
        record_evidence("transaction", txn, f"approvals.{n}.approver_id", "Who approved it."),
        record_evidence("transaction", txn, f"approvals.{n}.decided_at", "When they approved it."),
    ]
    is_bill = txn.kind is TransactionKind.VENDOR_BILL
    if is_bill and txn.paid_date and approval.decided_at.date() > txn.paid_date:
        evidence.append(record_evidence("transaction", txn, "paid_date", "When the bill was paid."))
        late = (approval.decided_at.date() - txn.paid_date).days
        return (
            CheckStatus.FAIL,
            IssueCode.APPROVAL_AFTER_THE_FACT,
            f"Approved by {name} on {_day(approval.decided_at)}, {late} days after the bill was "
            f"paid on {_day(txn.paid_date)}.",
            "The right person approved it, but only after the money had left. The approval "
            "did not control the payment.",
            evidence,
        )
    return (
        CheckStatus.PASS,
        None,
        f"Approved by {name} on {_day(approval.decided_at)}, as the rule requires.",
        "The rule applies at this amount and the person it names approved it.",
        evidence,
    )


def check_approval(txn: SpendTransaction, books: Books, run_id: str) -> CheckResult:
    def result(status, summary, rationale, evidence, issues=()):
        return CheckResult(
            id=result_id(txn.id, CheckName.APPROVAL),
            run_id=run_id,
            transaction_id=txn.id,
            check=CheckName.APPROVAL,
            status=status,
            issue_codes=list(issues),
            summary=summary,
            rationale=rationale,
            evidence=evidence,
        )

    policy = books.policy
    what = "bill" if txn.kind is TransactionKind.VENDOR_BILL else "card charge"
    amount = record_evidence("transaction", txn, "amount_cents", "The amount.")
    rules = list(enumerate(policy.approval_rules))
    applicable = [
        (index, rule)
        for index, rule in rules
        if txn.kind in rule.kinds and abs(txn.amount_cents) >= rule.min_amount_cents
    ]

    if not applicable:
        evidence = [amount]
        for index, rule in rules:
            if txn.kind in rule.kinds:
                field, note = "min_amount_cents", f"Rule '{rule.id}' starts at this amount."
            else:
                field, note = "kinds", f"Rule '{rule.id}' covers other kinds of spending."
            evidence.append(
                record_evidence("policy", policy, f"approval_rules.{index}.{field}", note)
            )
        if not rules:
            evidence.append(
                record_evidence(
                    "policy", policy, "approval_rules", "The policy has no approval rules."
                )
            )
        return result(
            CheckStatus.NOT_APPLICABLE,
            f"No approval rule applies to this {_money(txn.amount_cents)} {what}.",
            "Every approval rule in the policy either covers another kind of spending or "
            "starts at a higher amount.",
            evidence,
        )

    judged = sorted(
        (_judge(txn, index, rule, books, what) for index, rule in applicable),
        key=lambda outcome: _RANK[outcome[0]],
    )
    status, _, summary, rationale, _ = judged[0]
    issues = dict.fromkeys(issue for _, issue, *_ in judged if issue)
    evidence = [amount, *(item for *_, items in judged for item in items)]
    return result(status, summary, rationale, evidence, issues)
