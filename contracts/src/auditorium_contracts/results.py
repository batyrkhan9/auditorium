"""What the agent produces, and what the eval says about it.

The rule that shapes this file: a result is only as good as its evidence, and
evidence must be checkable by someone who does not trust the agent. So every
piece of evidence points at a real document page or a real record field, and
`auditorium_contracts.run.verify_evidence` can confirm it mechanically.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .records import AccountingPeriod, Record, Sha256, SourceSystem


class CheckName(StrEnum):
    RECEIPT = "receipt"
    APPROVAL = "approval"
    LEDGER_MATCH = "ledger_match"
    PERIOD = "period"


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"
    NOT_APPLICABLE = "not_applicable"


class IssueCode(StrEnum):
    """Why a check failed. Each code belongs to the check its name starts with.

    This list was written from the four checks in the PRD, before any mistake was
    planted. It says nothing about which mistakes the test data contains.
    """

    RECEIPT_MISSING = "receipt_missing"
    RECEIPT_MISMATCH = "receipt_mismatch"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_WRONG_APPROVER = "approval_wrong_approver"
    APPROVAL_AFTER_THE_FACT = "approval_after_the_fact"
    LEDGER_MATCH_MISSING = "ledger_match_missing"
    LEDGER_MATCH_AMOUNT = "ledger_match_amount"
    LEDGER_MATCH_VENDOR = "ledger_match_vendor"
    LEDGER_MATCH_DUPLICATE = "ledger_match_duplicate"
    PERIOD_WRONG = "period_wrong"
    OTHER = "other"


class DocumentEvidence(Record):
    """Words on a page. Verified by finding `quote` in the text of that page."""

    kind: Literal["document"] = "document"
    document_id: str
    page: int = Field(ge=1, description="1-based.")
    quote: str = Field(min_length=1, description="Copied from the page, not paraphrased.")
    note: str | None = Field(None, description="What this shows.")


class RecordEvidence(Record):
    """A field of a source record. Verified by reading that field and comparing."""

    kind: Literal["record"] = "record"
    record_type: Literal["transaction", "ledger_entry", "document", "person", "policy"]
    record_id: str
    field: str = Field(
        description="Dotted path into the record, e.g. 'txn_date', 'lines.0.account'."
    )
    value: str = Field(description="The value read. Strings as they are, anything else as JSON.")
    note: str | None = None


class SearchEvidence(Record):
    """A search and what it returned. This is how absence is shown.

    The ids it returned are verified. That nothing else matched cannot be
    verified from the output alone, so the log must hold the search itself.
    """

    kind: Literal["search"] = "search"
    system: SourceSystem
    query: str = Field(description="What was searched for, in words a reviewer can re-run.")
    result_ids: list[str] = Field(default_factory=list)
    note: str | None = None


Evidence = Annotated[
    DocumentEvidence | RecordEvidence | SearchEvidence, Field(discriminator="kind")
]


def result_id(transaction_id: str, check: CheckName | str) -> str:
    return f"{transaction_id}#{CheckName(check).value}"


class CheckResult(Record):
    """The outcome of one check on one transaction."""

    id: str = Field(description="result_id(transaction_id, check).")
    run_id: str
    transaction_id: str
    check: CheckName
    status: CheckStatus
    issue_codes: list[IssueCode] = Field(default_factory=list)
    summary: str = Field(description="One sentence an auditor can read on its own.")
    rationale: str = Field(description="How the evidence leads to the status.")
    evidence: list[Evidence] = Field(min_length=1)
    recorded_period: AccountingPeriod | None = Field(
        None, description="Period check: the period the books put it in."
    )
    expected_period: AccountingPeriod | None = Field(
        None, description="Period check: the period the evidence says it belongs to."
    )

    @model_validator(mode="after")
    def _consistent(self) -> CheckResult:
        if self.id != result_id(self.transaction_id, self.check):
            raise ValueError("id must be result_id(transaction_id, check)")
        if self.status is CheckStatus.FAIL and not self.issue_codes:
            raise ValueError("a failed check needs at least one issue code")
        if self.status in (CheckStatus.PASS, CheckStatus.NOT_APPLICABLE) and self.issue_codes:
            raise ValueError("a check that passed or did not apply carries no issue codes")
        for code in self.issue_codes:
            if code is not IssueCode.OTHER and not code.value.startswith(self.check.value):
                raise ValueError(
                    f"issue code {code.value} does not belong to check {self.check.value}"
                )
        if self.check is CheckName.PERIOD and self.status is CheckStatus.FAIL:
            if not (self.recorded_period and self.expected_period):
                raise ValueError("a failed period check states recorded_period and expected_period")
        return self


class PacketItem(Record):
    role: Literal["receipt_or_invoice", "approval", "ledger_entry", "payment", "policy", "other"]
    evidence: Evidence


class EvidencePacket(Record):
    """Everything lined up for one transaction the auditor picked."""

    id: str
    run_id: str
    sample_id: str
    sample_line: int
    transaction_id: str | None = Field(None, description="None if the line could not be matched.")
    items: list[PacketItem] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list, description="What could not be found.")
    result_ids: list[str] = Field(default_factory=list)
    draft_ids: list[str] = Field(default_factory=list)


class FollowUpDraft(Record):
    """A message asking a coworker for something missing. Always a draft."""

    id: str
    run_id: str
    channel: Literal["slack", "email"]
    to: str
    subject: str | None = None
    body: str
    asks_for: str = Field(description="The missing thing, in a few words.")
    result_ids: list[str] = Field(default_factory=list)
    sent: Literal[False] = Field(False, description="The agent never sends. A person does.")


class SourceUse(Record):
    """Whether a source was really connected for this run. This is the honesty field."""

    system: SourceSystem
    mode: Literal["live", "local"]
    detail: str = Field(description="The endpoint or the dataset path that was used.")


class RunManifest(Record):
    run_id: str
    mode: Literal["preflight", "sample"]
    started_at: datetime
    finished_at: datetime | None = None
    period_start: date = Field(description="First day of the period under audit.")
    period_end: date
    dataset_id: str | None = None
    dataset_path: str | None = Field(
        None, description="Where the dataset folder was when the run started, as it was given."
    )
    synthetic_data: bool = Field(
        False, description="True when the run read made-up data. The screen must say so."
    )
    sample_id: str | None = None
    sources: list[SourceUse]
    models: list[str] = Field(default_factory=list)
    git_commit: str | None = None
    contract_version: str


class ExpectedResult(Record):
    """One line of the answer key. The key has a line for every transaction and check."""

    transaction_id: str
    check: CheckName
    status: Literal[CheckStatus.PASS, CheckStatus.FAIL, CheckStatus.NOT_APPLICABLE]
    issue_codes: list[IssueCode] = Field(default_factory=list)


class CheckScore(Record):
    """Counts for one check. 'Positive' means the agent said fail."""

    true_positive: int = 0
    false_positive: int = 0
    false_negative: int = 0
    true_negative: int = 0
    needs_review: int = 0


class Scorecard(Record):
    """Aggregate scores for a run. It is safe to show to anyone: no transaction ids."""

    run_id: str
    dataset_id: str
    answer_key_sha256: Sha256
    scored_at: datetime
    per_check: dict[CheckName, CheckScore]
    evidence_total: int
    evidence_unverified: int = Field(
        description="Evidence that did not resolve to a real document or record. Must be 0."
    )
    notes: str | None = None
