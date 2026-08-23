"""Tests for core/ledger.py.

Ledger is an in-memory index over a loaded batch: O(1) exists()/get() by
(type, id) so the LLM reference checker (invariant 6) has a fast, precise
thing to check citations against, plus by_settlement() for the records that
belong to a given settlement cycle.
"""

from datetime import UTC, datetime

from core.ledger import Ledger
from core.models import (
    EntityType,
    FeeLine,
    FeeType,
    Payment,
    PaymentMethod,
    RecordRef,
    Refund,
)
from core.money import Money

UTC_NOON = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _payment(id_, settlement_id=None):
    return Payment(
        id=id_,
        merchant_id="m_1",
        amount=Money(1000),
        method=PaymentMethod.UPI,
        network=None,
        card_type=None,
        is_international=False,
        mcc="5411",
        captured_at=UTC_NOON,
        settlement_id=settlement_id,
    )


def _refund(id_, payment_id, settlement_id=None):
    return Refund(
        id=id_,
        payment_id=payment_id,
        amount=Money(100),
        is_partial=False,
        created_at=UTC_NOON,
        settlement_id=settlement_id,
    )


def _fee_line(id_, applies_to_id):
    return FeeLine(
        id=id_,
        applies_to_id=applies_to_id,
        applies_to_type=EntityType.PAYMENT,
        fee_type=FeeType.MDR,
        computed_amount=Money(24),
        rule_id="rule_1",
    )


# ---------------------------------------------------------------------------
# exists() / get()
# ---------------------------------------------------------------------------


def test_exists_true_for_loaded_record():
    ledger = Ledger([_payment("pay_1")])
    assert ledger.exists(RecordRef(type=EntityType.PAYMENT, id="pay_1")) is True


def test_exists_false_for_absent_id():
    ledger = Ledger([_payment("pay_1")])
    assert ledger.exists(RecordRef(type=EntityType.PAYMENT, id="pay_absent")) is False


def test_exists_false_for_right_id_wrong_type():
    ledger = Ledger([_payment("pay_1")])
    assert ledger.exists(RecordRef(type=EntityType.REFUND, id="pay_1")) is False


def test_get_returns_the_record():
    payment = _payment("pay_1")
    ledger = Ledger([payment])
    assert ledger.get(RecordRef(type=EntityType.PAYMENT, id="pay_1")) == payment


def test_get_returns_none_for_absent_ref():
    ledger = Ledger([_payment("pay_1")])
    assert ledger.get(RecordRef(type=EntityType.PAYMENT, id="pay_absent")) is None


# ---------------------------------------------------------------------------
# by_settlement()
# ---------------------------------------------------------------------------


def test_by_settlement_groups_records_across_types():
    ledger = Ledger(
        [
            _payment("pay_1", settlement_id="batch_1"),
            _payment("pay_2", settlement_id="batch_1"),
            _refund("ref_1", payment_id="pay_1", settlement_id="batch_1"),
            _payment("pay_3", settlement_id="batch_2"),
        ]
    )
    refs = ledger.by_settlement("batch_1")
    assert set(refs) == {
        RecordRef(type=EntityType.PAYMENT, id="pay_1"),
        RecordRef(type=EntityType.PAYMENT, id="pay_2"),
        RecordRef(type=EntityType.REFUND, id="ref_1"),
    }


def test_by_settlement_empty_for_unknown_settlement_id():
    ledger = Ledger([_payment("pay_1", settlement_id="batch_1")])
    assert ledger.by_settlement("unknown_batch") == []


def test_by_settlement_excludes_records_without_settlement_id_field():
    ledger = Ledger([_fee_line("fee_1", applies_to_id="pay_1")])
    assert ledger.by_settlement("batch_1") == []
    assert ledger.exists(RecordRef(type=EntityType.FEE_LINE, id="fee_1")) is True


def test_by_settlement_excludes_records_with_none_settlement_id():
    ledger = Ledger([_payment("pay_1", settlement_id=None)])
    assert ledger.by_settlement("batch_1") == []
