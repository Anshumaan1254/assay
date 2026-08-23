"""Tests for core/models.py.

Entities are frozen Pydantic v2 models. Money fields delegate validation
entirely to Money (via a PlainValidator, so Pydantic's own int/float
coercion never gets a chance to accept something Money would reject).
Every timestamp is normalized to IST on the way in, so two audit runs over
the same inputs never disagree about wall-clock time — see the IST section
below for why a naive datetime is rejected rather than assumed.
"""

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from core.exceptions import DiscrepancyClass
from core.models import (
    IST,
    Adjustment,
    AdjustmentKind,
    AuditRun,
    BankCredit,
    BatchStatus,
    CardType,
    Chargeback,
    ChargebackStage,
    EntityType,
    FeeLine,
    FeeType,
    Finding,
    JournalEntry,
    Lane,
    Network,
    Payment,
    PaymentMethod,
    RecordRef,
    Refund,
    SettlementBatch,
    Severity,
    TaxLine,
)
from core.money import Money

UTC_NOON = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _payment(**overrides):
    fields = {
        "id": "pay_1",
        "merchant_id": "m_1",
        "amount": Money(10000),
        "method": PaymentMethod.UPI,
        "network": None,
        "card_type": None,
        "is_international": False,
        "mcc": "5411",
        "captured_at": UTC_NOON,
        "settlement_id": None,
    }
    fields.update(overrides)
    return Payment(**fields)


def _finding(**overrides):
    fields = {
        "id": "find_1",
        "audit_run_id": "run_1",
        "discrepancy_class": DiscrepancyClass.FEE_OVERCHARGE,
        "severity": Severity.MINOR,
        "amount_impact": Money(1),
        "evidence_ids": [],
        "confidence": 1,
        "lane": Lane.AUTO,
        "explanation": "x",
    }
    fields.update(overrides)
    return Finding(**fields)


# ---------------------------------------------------------------------------
# Construction — one happy path per entity
# ---------------------------------------------------------------------------


def test_construct_payment():
    p = _payment(network=Network.VISA, card_type=CardType.CREDIT, method=PaymentMethod.CARD)
    assert p.amount == Money(10000)
    assert p.network is Network.VISA
    assert p.settlement_id is None


def test_construct_refund():
    r = Refund(
        id="ref_1",
        payment_id="pay_1",
        amount=Money(2000),
        is_partial=True,
        created_at=UTC_NOON,
        settlement_id="batch_1",
    )
    assert r.amount == Money(2000)
    assert r.is_partial is True


def test_construct_chargeback():
    c = Chargeback(
        id="cb_1",
        payment_id="pay_1",
        amount=Money(10000),
        reason_code="10.4",
        stage=ChargebackStage.RAISED,
        raised_at=UTC_NOON,
        resolved_at=None,
        settlement_id=None,
    )
    assert c.stage is ChargebackStage.RAISED
    assert c.resolved_at is None


def test_construct_adjustment():
    a = Adjustment(
        id="adj_1",
        kind=AdjustmentKind.RESERVE_HOLD,
        amount=Money(5000),
        reason="rolling reserve",
        created_at=UTC_NOON,
        settlement_id="batch_1",
    )
    assert a.kind is AdjustmentKind.RESERVE_HOLD


def test_construct_fee_line():
    f = FeeLine(
        id="fee_1",
        applies_to_id="pay_1",
        applies_to_type=EntityType.PAYMENT,
        fee_type=FeeType.MDR,
        computed_amount=Money(236),
        rule_id="rule_mdr_v1",
    )
    assert f.applies_to_ref == RecordRef(type=EntityType.PAYMENT, id="pay_1")


def test_construct_tax_line():
    t = TaxLine(
        id="tax_1",
        applies_to_fee_id="fee_1",
        tax_type="gst",
        rate_bps=1800,
        base_amount=Money(236),
        amount=Money(42),
    )
    assert t.rate_bps == 1800


def test_construct_settlement_batch():
    b = SettlementBatch(
        id="batch_1",
        merchant_id="m_1",
        cycle_start=UTC_NOON,
        cycle_end=UTC_NOON,
        expected_credit=Money(1_000_000),
        utr="UTR123",
        status=BatchStatus.PENDING,
    )
    assert b.status is BatchStatus.PENDING


def test_construct_bank_credit():
    bc = BankCredit(
        id="credit_1",
        utr="UTR123",
        amount=Money(998_000),
        value_date=date(2026, 8, 23),
        narration="NEFT/UTR123/RAZORPAY SOFTWARE",
    )
    assert bc.value_date == date(2026, 8, 23)


def test_construct_audit_run():
    run = AuditRun(
        id="run_1",
        started_at=UTC_NOON,
        contract_version="v1",
        input_hashes={"ledger": "abc123"},
        seed=42,
        report_hash="def456",
    )
    assert run.seed == 42
    assert run.input_hashes == {"ledger": "abc123"}


def test_construct_finding():
    finding = _finding(
        discrepancy_class=DiscrepancyClass.FEE_OVERCHARGE,
        severity=Severity.MAJOR,
        amount_impact=Money(150),
        evidence_ids=[RecordRef(type=EntityType.FEE_LINE, id="fee_1")],
        confidence=8500,
        lane=Lane.PROPOSE,
        explanation="MDR charged at 2.5% vs contracted 2.36%",
    )
    assert finding.confidence == 8500
    assert finding.lane is Lane.PROPOSE
    assert finding.discrepancy_class is DiscrepancyClass.FEE_OVERCHARGE


def test_finding_class_field_has_no_reserved_word_conflict():
    # "class" is a reserved word and cannot be a Python attribute name, so
    # the field is `discrepancy_class` everywhere — Python and JSON both,
    # no alias to keep in sync.
    finding = _finding(discrepancy_class=DiscrepancyClass.TAX_MISCALCULATION)
    assert finding.discrepancy_class is DiscrepancyClass.TAX_MISCALCULATION
    assert finding.model_dump()["discrepancy_class"] == "tax_miscalculation"


def test_construct_journal_entry():
    je = JournalEntry(
        id="je_1",
        debit_account="merchant_payable",
        credit_account="bank_clearing",
        amount=Money(998_000),
        narration="settlement batch_1",
        idempotency_key="run_1:batch_1",
        posted_at=UTC_NOON,
    )
    assert je.amount == Money(998_000)


# ---------------------------------------------------------------------------
# Immutability — frozen models reject attribute assignment
# ---------------------------------------------------------------------------


def test_payment_is_frozen():
    p = _payment()
    with pytest.raises(ValidationError):
        p.amount = Money(200)


def test_finding_is_frozen():
    finding = _finding()
    with pytest.raises(ValidationError):
        finding.severity = Severity.CRITICAL


# ---------------------------------------------------------------------------
# IST normalization
# ---------------------------------------------------------------------------


def test_ist_normalization_converts_utc_to_ist():
    p = _payment(captured_at=UTC_NOON)
    assert p.captured_at.tzinfo == IST
    assert (p.captured_at.hour, p.captured_at.minute) == (17, 30)


def test_ist_normalization_already_ist_unchanged():
    ist_dt = datetime(2026, 8, 23, 17, 30, tzinfo=IST)
    p = _payment(captured_at=ist_dt)
    assert p.captured_at == ist_dt


def test_ist_normalization_rejects_naive_datetime():
    naive = datetime(2026, 8, 23, 12, 0)  # noqa: DTZ001 - deliberately naive
    with pytest.raises(ValidationError):
        _payment(captured_at=naive)


# ---------------------------------------------------------------------------
# Money fields — validation delegates entirely to Money
# ---------------------------------------------------------------------------


def test_money_field_accepts_dict():
    p = _payment(amount={"paise": 100, "currency": "INR"})
    assert p.amount == Money(100)


def test_money_field_accepts_money_instance():
    p = _payment(amount=Money(100))
    assert p.amount == Money(100)


def test_money_field_rejects_float():
    with pytest.raises(TypeError):
        _payment(amount=100.5)


def test_money_field_rejects_unrelated_type():
    with pytest.raises(TypeError):
        _payment(amount="100.00")


# ---------------------------------------------------------------------------
# RecordRef / EntityType
# ---------------------------------------------------------------------------


def test_record_ref_construction():
    ref = RecordRef(type=EntityType.PAYMENT, id="pay_1")
    assert ref.type is EntityType.PAYMENT
    assert ref.id == "pay_1"


def test_record_ref_hashable_and_usable_in_sets():
    a = RecordRef(type=EntityType.PAYMENT, id="pay_1")
    b = RecordRef(type=EntityType.PAYMENT, id="pay_1")
    c = RecordRef(type=EntityType.REFUND, id="pay_1")
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b, c}) == 2
