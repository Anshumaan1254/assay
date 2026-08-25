"""Domain entities.

All frozen: a state change creates a new record (a new id, or a caller-side
`model_copy(update=...)`), it never mutates one in place — the same
append-only discipline invariant 7 requires of the ledger applies to every
entity that can end up cited as evidence.

Every Money field is typed `PaisaAmount`, which delegates validation
entirely to `Money` itself via a PlainValidator — Pydantic never gets a
chance to apply its own int/float coercion to `paise`/`currency`, so a bool
or float that `Money.__post_init__` would reject stays rejected here too.

Every timestamp is `ISTDatetime`: any timezone-aware datetime is accepted
and normalized to Asia/Kolkata, but a naive one is refused outright rather
than assumed to be some particular zone.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, ClassVar
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer, PlainValidator

from core.money import Money

IST = ZoneInfo("Asia/Kolkata")


def _to_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(IST)


ISTDatetime = Annotated[datetime, AfterValidator(_to_ist)]


def _coerce_money(value: object) -> Money:
    if isinstance(value, Money):
        return value
    if isinstance(value, dict):
        return Money(**value)
    raise TypeError(f"expected Money or dict, got {type(value).__name__}")


def _serialize_money(value: Money) -> dict:
    return {"paise": value.paise, "currency": value.currency}


PaisaAmount = Annotated[
    Money,
    PlainValidator(_coerce_money),
    PlainSerializer(_serialize_money, return_type=dict),
]


class AssayModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EntityType(StrEnum):
    PAYMENT = "payment"
    REFUND = "refund"
    CHARGEBACK = "chargeback"
    ADJUSTMENT = "adjustment"
    FEE_LINE = "fee_line"
    TAX_LINE = "tax_line"
    SETTLEMENT_BATCH = "settlement_batch"
    BANK_CREDIT = "bank_credit"
    AUDIT_RUN = "audit_run"
    FINDING = "finding"
    JOURNAL_ENTRY = "journal_entry"


class RecordRef(AssayModel):
    """A (type, id) pair. Every evidence citation uses this, so the LLM
    reference checker (invariant 6) has exactly one shape to validate
    against, and `Ledger.exists()` has exactly one shape to look up."""

    type: EntityType
    id: str


class PaymentMethod(StrEnum):
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMI = "emi"


class Network(StrEnum):
    VISA = "visa"
    MASTERCARD = "mastercard"
    RUPAY = "rupay"
    AMEX = "amex"


class CardType(StrEnum):
    CREDIT = "credit"
    DEBIT = "debit"
    PREPAID = "prepaid"


class Payment(AssayModel):
    RECORD_TYPE: ClassVar[EntityType] = EntityType.PAYMENT

    id: str
    merchant_id: str
    amount: PaisaAmount
    method: PaymentMethod
    network: Network | None
    card_type: CardType | None
    is_international: bool
    mcc: str
    captured_at: ISTDatetime
    settlement_id: str | None


class Refund(AssayModel):
    RECORD_TYPE: ClassVar[EntityType] = EntityType.REFUND

    id: str
    payment_id: str
    amount: PaisaAmount
    is_partial: bool
    created_at: ISTDatetime
    settlement_id: str | None


class ChargebackStage(StrEnum):
    RAISED = "raised"
    REPRESENTED = "represented"
    WON = "won"
    LOST = "lost"


class Chargeback(AssayModel):
    RECORD_TYPE: ClassVar[EntityType] = EntityType.CHARGEBACK

    id: str
    payment_id: str
    amount: PaisaAmount
    reason_code: str
    stage: ChargebackStage
    raised_at: ISTDatetime
    resolved_at: ISTDatetime | None
    settlement_id: str | None


class AdjustmentKind(StrEnum):
    RESERVE_HOLD = "reserve_hold"
    RESERVE_RELEASE = "reserve_release"
    MANUAL_CREDIT = "manual_credit"
    MANUAL_DEBIT = "manual_debit"
    FEE_WAIVER = "fee_waiver"


class Adjustment(AssayModel):
    RECORD_TYPE: ClassVar[EntityType] = EntityType.ADJUSTMENT

    id: str
    kind: AdjustmentKind
    amount: PaisaAmount
    reason: str
    created_at: ISTDatetime
    settlement_id: str | None


class FeeType(StrEnum):
    MDR = "mdr"
    FIXED = "fixed"
    INTERNATIONAL = "international"
    EMI_SUBVENTION = "emi_subvention"
    DISPUTE_FEE = "dispute_fee"


class FeeLine(AssayModel):
    RECORD_TYPE: ClassVar[EntityType] = EntityType.FEE_LINE

    id: str
    applies_to_id: str
    applies_to_type: EntityType
    fee_type: FeeType
    computed_amount: PaisaAmount
    rule_id: str

    @property
    def applies_to_ref(self) -> RecordRef:
        return RecordRef(type=self.applies_to_type, id=self.applies_to_id)


class TaxLine(AssayModel):
    RECORD_TYPE: ClassVar[EntityType] = EntityType.TAX_LINE

    id: str
    applies_to_fee_id: str
    tax_type: str
    rate_bps: int
    base_amount: PaisaAmount
    amount: PaisaAmount


class BatchStatus(StrEnum):
    PENDING = "pending"
    PARTIALLY_SETTLED = "partially_settled"
    SETTLED = "settled"
    SHORT_SETTLED = "short_settled"
    DISPUTED = "disputed"
    CLOSED = "closed"


class SettlementBatch(AssayModel):
    RECORD_TYPE: ClassVar[EntityType] = EntityType.SETTLEMENT_BATCH

    id: str
    merchant_id: str
    cycle_start: ISTDatetime
    cycle_end: ISTDatetime
    expected_credit: PaisaAmount
    utr: str
    status: BatchStatus


class BankCredit(AssayModel):
    RECORD_TYPE: ClassVar[EntityType] = EntityType.BANK_CREDIT

    id: str
    utr: str
    amount: PaisaAmount
    value_date: date
    narration: str


class AuditRun(AssayModel):
    RECORD_TYPE: ClassVar[EntityType] = EntityType.AUDIT_RUN

    id: str
    started_at: ISTDatetime
    contract_version: str
    input_hashes: dict[str, str]
    seed: int
    report_hash: str


class Severity(StrEnum):
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


class Lane(StrEnum):
    AUTO = "auto"
    PROPOSE = "propose"
    ESCALATE = "escalate"


# Imported here, immediately before Finding, rather than at module top:
# core/exceptions.py's clustering/pricing code needs AssayModel, EntityType,
# RecordRef and PaisaAmount back from this module, which would otherwise be
# a genuine import cycle (models -> exceptions -> models) if this import
# ran before any of those names existed yet. By the time Python reaches
# this line, they're already defined above, so exceptions.py's own import
# of them succeeds regardless of which of the two modules loads first.
from core.exceptions import DiscrepancyClass


class Finding(AssayModel):
    # "class" is a reserved word and cannot be a Python attribute name, so
    # the field is `discrepancy_class` everywhere — Python and JSON both.
    # No alias: an aliased name only serializes as "class" when every call
    # site remembers `by_alias=True`, and forgetting it once would silently
    # emit the wrong key in a report.
    RECORD_TYPE: ClassVar[EntityType] = EntityType.FINDING

    id: str
    audit_run_id: str
    discrepancy_class: DiscrepancyClass
    severity: Severity
    amount_impact: PaisaAmount
    evidence_ids: list[RecordRef]
    confidence: int = Field(ge=0, le=10000)
    lane: Lane
    explanation: str


class JournalEntry(AssayModel):
    RECORD_TYPE: ClassVar[EntityType] = EntityType.JOURNAL_ENTRY

    id: str
    debit_account: str
    credit_account: str
    amount: PaisaAmount
    narration: str
    idempotency_key: str
    posted_at: ISTDatetime
