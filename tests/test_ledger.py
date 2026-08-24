"""Tests for core/ledger.py.

Ledger is an in-memory index over a loaded batch: O(1) exists()/get() by
(type, id) so the LLM reference checker (invariant 6) has a fast, precise
thing to check citations against, plus by_settlement() for the records that
belong to a given settlement cycle.

The by_utr / by_type / fee_lines_for / tax_lines_for indexes exist for
core/decompose.py: BankCredit has no settlement_id, so its only structural
link to a SettlementBatch is the UTR, and FeeLine/TaxLine have no
settlement_id either -- they attach transitively via applies_to_id and
applies_to_fee_id.
"""

from datetime import UTC, date, datetime

from core.ledger import Ledger
from core.models import (
    BankCredit,
    BatchStatus,
    EntityType,
    FeeLine,
    FeeType,
    Payment,
    PaymentMethod,
    RecordRef,
    Refund,
    SettlementBatch,
    TaxLine,
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


def test_by_settlement_is_sorted():
    # Insertion order is not reproducible across input orderings, and
    # decompose sums these refs into a proof whose hash must be stable
    # (invariant 4). Sorted output makes the sum order deterministic.
    ledger = Ledger(
        [
            _payment("pay_3", settlement_id="batch_1"),
            _payment("pay_1", settlement_id="batch_1"),
            _payment("pay_2", settlement_id="batch_1"),
        ]
    )
    assert ledger.by_settlement("batch_1") == sorted(ledger.by_settlement("batch_1"), key=_sort_key)


# ---------------------------------------------------------------------------
# by_utr() -- the only structural link from a BankCredit to a SettlementBatch
# ---------------------------------------------------------------------------


def _batch(id_, utr, merchant_id="MERCH-0001"):
    return SettlementBatch(
        id=id_,
        merchant_id=merchant_id,
        cycle_start=UTC_NOON,
        cycle_end=UTC_NOON,
        expected_credit=Money(100_000),
        utr=utr,
        status=BatchStatus.SETTLED,
    )


def _bank_credit(id_, utr, paise=100_000, value_date=date(2026, 8, 25)):
    return BankCredit(
        id=id_,
        utr=utr,
        amount=Money(paise),
        value_date=value_date,
        narration=f"NEFT settlement {utr}",
    )


def _tax_line(id_, applies_to_fee_id, paise=425):
    return TaxLine(
        id=id_,
        applies_to_fee_id=applies_to_fee_id,
        tax_type="GST",
        rate_bps=1800,
        base_amount=Money(2_360),
        amount=Money(paise),
    )


def _sort_key(ref):
    return (ref.type, ref.id)


def test_by_utr_finds_the_batch():
    ledger = Ledger([_batch("STL-1", "HDFC0001")])
    assert ledger.by_utr("HDFC0001") == [RecordRef(type=EntityType.SETTLEMENT_BATCH, id="STL-1")]


def test_by_utr_returns_both_the_batch_and_the_credit():
    # Both entities carry a utr. by_utr does not filter by type -- callers
    # that want one kind say so, and decompose does.
    ledger = Ledger([_batch("STL-1", "HDFC0001"), _bank_credit("BC-1", "HDFC0001")])
    assert set(ledger.by_utr("HDFC0001")) == {
        RecordRef(type=EntityType.SETTLEMENT_BATCH, id="STL-1"),
        RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
    }


def test_by_utr_surfaces_two_batches_sharing_a_utr():
    # Must not silently dedupe. Two batches on one UTR is a real condition
    # and decompose refuses to resolve structurally when it sees it, rather
    # than picking whichever the index happened to keep.
    ledger = Ledger([_batch("STL-1", "HDFC0001"), _batch("STL-2", "HDFC0001")])
    refs = ledger.by_utr("HDFC0001")
    assert len(refs) == 2


def test_by_utr_empty_for_unknown_utr():
    assert Ledger([_batch("STL-1", "HDFC0001")]).by_utr("HDFC9999") == []


def test_by_utr_empty_for_empty_string():
    # A blank UTR is the "no structural key" case, not a wildcard that
    # matches every record lacking one.
    assert Ledger([_batch("STL-1", "HDFC0001")]).by_utr("") == []


def test_by_utr_does_not_index_records_without_a_utr_field():
    ledger = Ledger([_payment("pay_1")])
    assert ledger.by_utr("HDFC0001") == []


def test_by_utr_is_sorted():
    ledger = Ledger([_batch("STL-3", "U"), _batch("STL-1", "U"), _batch("STL-2", "U")])
    assert ledger.by_utr("U") == sorted(ledger.by_utr("U"), key=_sort_key)


# ---------------------------------------------------------------------------
# by_type()
# ---------------------------------------------------------------------------


def test_by_type_returns_only_that_type():
    ledger = Ledger([_payment("pay_1"), _refund("ref_1", "pay_1"), _payment("pay_2")])
    assert ledger.by_type(EntityType.PAYMENT) == [
        RecordRef(type=EntityType.PAYMENT, id="pay_1"),
        RecordRef(type=EntityType.PAYMENT, id="pay_2"),
    ]


def test_by_type_empty_for_absent_type():
    assert Ledger([_payment("pay_1")]).by_type(EntityType.CHARGEBACK) == []


def test_by_type_is_sorted():
    ledger = Ledger([_payment("pay_3"), _payment("pay_1"), _payment("pay_2")])
    ids = [ref.id for ref in ledger.by_type(EntityType.PAYMENT)]
    assert ids == ["pay_1", "pay_2", "pay_3"]


# ---------------------------------------------------------------------------
# fee_lines_for() / tax_lines_for() -- the transitive join. FeeLine and
# TaxLine have no settlement_id, so by_settlement() will never return them;
# a decomposition that only used by_settlement() would omit every deduction.
# ---------------------------------------------------------------------------


def test_fee_lines_for_finds_fees_on_a_payment():
    ledger = Ledger([_payment("pay_1"), _fee_line("fee_1", "pay_1"), _fee_line("fee_2", "pay_1")])
    assert ledger.fee_lines_for(RecordRef(type=EntityType.PAYMENT, id="pay_1")) == [
        RecordRef(type=EntityType.FEE_LINE, id="fee_1"),
        RecordRef(type=EntityType.FEE_LINE, id="fee_2"),
    ]


def test_fee_lines_for_matches_on_type_as_well_as_id():
    # A dispute fee applies to a CHARGEBACK; a payment with a colliding id
    # must not pick it up. applies_to_type exists precisely for this.
    fee = FeeLine(
        id="fee_1",
        applies_to_id="shared_id",
        applies_to_type=EntityType.CHARGEBACK,
        fee_type=FeeType.DISPUTE_FEE,
        computed_amount=Money(50_000),
        rule_id="rule_cb",
    )
    ledger = Ledger([fee])
    assert ledger.fee_lines_for(RecordRef(type=EntityType.PAYMENT, id="shared_id")) == []
    assert ledger.fee_lines_for(RecordRef(type=EntityType.CHARGEBACK, id="shared_id")) == [
        RecordRef(type=EntityType.FEE_LINE, id="fee_1")
    ]


def test_fee_lines_for_empty_when_none_attach():
    ledger = Ledger([_payment("pay_1"), _fee_line("fee_1", "pay_2")])
    assert ledger.fee_lines_for(RecordRef(type=EntityType.PAYMENT, id="pay_1")) == []


def test_tax_lines_for_finds_taxes_on_a_fee():
    ledger = Ledger([_fee_line("fee_1", "pay_1"), _tax_line("tax_1", "fee_1")])
    assert ledger.tax_lines_for(RecordRef(type=EntityType.FEE_LINE, id="fee_1")) == [
        RecordRef(type=EntityType.TAX_LINE, id="tax_1")
    ]


def test_tax_lines_for_empty_for_a_non_fee_ref():
    ledger = Ledger([_fee_line("fee_1", "pay_1"), _tax_line("tax_1", "fee_1")])
    assert ledger.tax_lines_for(RecordRef(type=EntityType.PAYMENT, id="fee_1")) == []


def test_fee_and_tax_lines_are_sorted():
    ledger = Ledger([_fee_line("fee_3", "pay_1"), _fee_line("fee_1", "pay_1"), _fee_line("fee_2", "pay_1")])
    ids = [ref.id for ref in ledger.fee_lines_for(RecordRef(type=EntityType.PAYMENT, id="pay_1"))]
    assert ids == ["fee_1", "fee_2", "fee_3"]


# ---------------------------------------------------------------------------
# __len__ / __iter__ -- the proof verifier walks the whole ledger
# ---------------------------------------------------------------------------


def test_len_counts_loaded_records():
    assert len(Ledger([_payment("pay_1"), _refund("ref_1", "pay_1")])) == 2


def test_len_of_empty_ledger():
    assert len(Ledger([])) == 0


def test_iter_yields_every_ref_sorted():
    ledger = Ledger([_refund("ref_1", "pay_1"), _payment("pay_1")])
    assert list(ledger) == [
        RecordRef(type=EntityType.PAYMENT, id="pay_1"),
        RecordRef(type=EntityType.REFUND, id="ref_1"),
    ]
