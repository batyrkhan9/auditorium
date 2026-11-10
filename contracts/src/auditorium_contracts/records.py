"""What the source systems hold, in one canonical shape.

These records carry only what Ramp, QuickBooks and the company really provide.
Anything that takes judgement is deliberately absent: the period a service was
delivered in, or which ledger entry belongs to which transaction. Working that
out from the documents is the agent's job, and a field that handed it over would
make the checks meaningless.

Money is integer US cents. Ids are opaque strings, unique within their kind.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# An accounting period is a calendar month, written YYYY-MM.
AccountingPeriod = Annotated[str, Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


def period_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceSystem(StrEnum):
    RAMP = "ramp"
    QBO = "qbo"
    BANK = "bank"
    AUDITOR = "auditor"
    COMPANY = "company"


class SourceRef(Record):
    """Where a record or document lives in its home system."""

    system: SourceSystem
    record_type: str = Field(description="The home system's own name for it, e.g. 'bill', 'Bill'.")
    record_id: str


class Person(Record):
    id: str
    name: str
    email: str | None = None
    role: str | None = Field(None, description="Job role, e.g. 'cfo'. Approval rules match on it.")
    department: str | None = None
    manager_id: str | None = None


class Approval(Record):
    approver_id: str = Field(description="Person.id of whoever decided.")
    decision: Literal["approved", "rejected"]
    decided_at: datetime


class TransactionKind(StrEnum):
    CARD_EXPENSE = "card_expense"
    VENDOR_BILL = "vendor_bill"


class SpendTransaction(Record):
    """One card expense or vendor bill, as the card platform (Ramp) holds it."""

    id: str
    kind: TransactionKind
    source: SourceRef
    vendor_name: str
    amount_cents: int = Field(
        description="Positive is money spent. Negative is a refund or credit."
    )
    transaction_date: date = Field(description="Card: the purchase date. Bill: the invoice date.")
    accounting_date: date | None = Field(None, description="The date Ramp assigned for the books.")
    paid_date: date | None = None
    due_date: date | None = None
    invoice_number: str | None = None
    memo: str | None = None
    category: str | None = Field(None, description="The accounting category coded in Ramp.")
    spender_id: str | None = Field(None, description="Person.id of the cardholder or requester.")
    department: str | None = None
    document_ids: list[str] = Field(
        default_factory=list, description="Receipts or invoices attached in Ramp."
    )
    approvals: list[Approval] = Field(default_factory=list)
    raw: dict[str, Any] | None = Field(None, description="The record as the source returned it.")


class LedgerEntryType(StrEnum):
    BILL = "bill"
    EXPENSE = "expense"
    BILL_PAYMENT = "bill_payment"
    JOURNAL_ENTRY = "journal_entry"
    VENDOR_CREDIT = "vendor_credit"
    OTHER = "other"


class LedgerLine(Record):
    account: str
    side: Literal["debit", "credit"]
    amount_cents: int = Field(ge=0)
    description: str | None = None
    department: str | None = None


class LedgerEntry(Record):
    """One entry in the accounting system (QuickBooks). `txn_date` sets its period."""

    id: str
    source: SourceRef
    entry_type: LedgerEntryType
    txn_date: date
    vendor_name: str | None = None
    doc_number: str | None = None
    memo: str | None = None
    total_cents: int
    lines: list[LedgerLine] = Field(default_factory=list)
    raw: dict[str, Any] | None = None


class DocumentType(StrEnum):
    RECEIPT = "receipt"
    INVOICE = "invoice"
    BANK_STATEMENT = "bank_statement"
    PBC_LIST = "pbc_list"
    SAMPLE_REQUEST = "sample_request"
    POLICY = "policy"
    CONTRACT = "contract"
    OTHER = "other"


class Document(Record):
    """A file. `type` is what the source called it, which is not proof of what it is."""

    id: str
    type: DocumentType
    source: SourceRef
    origin: Literal["external", "internal"] = Field(
        description="External: produced outside the company (vendor, bank, auditor). "
        "AU-C 500 treats external evidence as more reliable."
    )
    path: str = Field(description="POSIX path to the file, relative to the dataset folder.")
    media_type: str = "application/pdf"
    sha256: Sha256
    filename: str | None = None


class ApprovalRule(Record):
    id: str
    description: str
    kinds: list[TransactionKind]
    min_amount_cents: int = Field(description="The rule applies at or above this amount.")
    approver_role: str = Field(
        description="'manager' means the spender's manager (Person.manager_id). "
        "Anything else is matched against Person.role."
    )


class SpendPolicy(Record):
    """The company's own rules. The receipt and approval checks are judged against these."""

    id: str
    effective_from: date
    effective_to: date | None = None
    receipt_required_from_cents: int = Field(
        description="A card expense at or above this needs a receipt. A vendor bill always "
        "needs its invoice."
    )
    approval_rules: list[ApprovalRule]
    document_id: str | None = Field(None, description="The written policy, if there is one.")


class DatasetManifest(Record):
    dataset_id: str
    company_name: str
    contract_version: str
    fiscal_year_start: date
    fiscal_year_end: date
    coverage_end: date = Field(
        description="Records run through this date. It is after the fiscal year end, because "
        "cut-off testing needs the weeks that follow it."
    )
    description: str | None = None


class SampleItem(Record):
    """One line of an auditor's selection, as the auditor wrote it. No internal ids."""

    line: int
    description: str
    transaction_date: date | None = None
    vendor_name: str | None = None
    amount_cents: int | None = None
    reference: str | None = None


class SampleRequest(Record):
    """The transactions an auditor picked to test."""

    id: str
    auditor: str
    received_on: date
    items: list[SampleItem]
    document_id: str | None = Field(None, description="The request as received, if it was a file.")
