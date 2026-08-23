"""Money-critical: the true world's conservation identity holds exactly,
per batch, with zero tolerance, before any discrepancy is injected. Also
covers same-seed determinism and the realism properties the spec calls out
by name (cross-cycle refunds, won-chargeback reversals).

Written before datagen/world.py exists -- expected to fail on collection
until that module is implemented.
"""

from __future__ import annotations

from collections import defaultdict
from random import Random

import pytest

from core.models import AdjustmentKind, BatchStatus, ChargebackStage, EntityType
from datagen.config import GenerationConfig
from datagen.ratecard import default_rate_card
from datagen.world import build_true_world

# A smaller-than-production config keeps these tests fast; the money
# invariants they check don't depend on scale.
_SMALL_CONFIG = GenerationConfig(month="2026-07", total_payments=600)


def _build(seed: int = 42, config: GenerationConfig = _SMALL_CONFIG):
    rng = Random(seed)
    rate_card = default_rate_card(month=config.month)
    return build_true_world(config, rate_card, rng)


# ---------------------------------------------------------------------------
# Conservation identity: recompute each batch's net independently from the
# raw records and assert it equals both SettlementBatch.expected_credit and
# the matching BankCredit.amount, exactly, in paise.
# ---------------------------------------------------------------------------

_ADD_BACK_KINDS = {AdjustmentKind.RESERVE_RELEASE, AdjustmentKind.MANUAL_CREDIT, AdjustmentKind.FEE_WAIVER}
_SUBTRACT_KINDS = {AdjustmentKind.RESERVE_HOLD, AdjustmentKind.MANUAL_DEBIT}


def _recompute_expected_credit_paise(world, batch_id: str) -> int:
    payments_in_batch = {p.id for p in world.payments if p.settlement_id == batch_id}
    gross = sum(p.amount.paise for p in world.payments if p.settlement_id == batch_id)
    fees = sum(f.computed_amount.paise for f in world.fee_lines if f.applies_to_id in payments_in_batch)
    fee_ids_in_batch = {f.id for f in world.fee_lines if f.applies_to_id in payments_in_batch}
    tax = sum(t.amount.paise for t in world.tax_lines if t.applies_to_fee_id in fee_ids_in_batch)
    refunds = sum(r.amount.paise for r in world.refunds if r.settlement_id == batch_id)
    chargebacks = sum(c.amount.paise for c in world.chargebacks if c.settlement_id == batch_id)
    add_back = sum(a.amount.paise for a in world.adjustments if a.settlement_id == batch_id and a.kind in _ADD_BACK_KINDS)
    subtract = sum(a.amount.paise for a in world.adjustments if a.settlement_id == batch_id and a.kind in _SUBTRACT_KINDS)
    return gross - refunds - fees - tax - chargebacks + add_back - subtract


def test_conservation_identity_holds_exactly_per_batch():
    world = _build()
    assert world.batches, "expected at least one settlement batch"
    bank_credit_by_utr = {bc.utr: bc for bc in world.bank_credits}
    for batch in world.batches:
        recomputed = _recompute_expected_credit_paise(world, batch.id)
        assert recomputed == batch.expected_credit.paise, f"batch {batch.id}: recomputed {recomputed} != expected_credit {batch.expected_credit.paise}"
        credit = bank_credit_by_utr[batch.utr]
        assert credit.amount.paise == batch.expected_credit.paise, f"batch {batch.id}: bank credit {credit.amount.paise} != expected_credit {batch.expected_credit.paise}"


def test_every_batch_status_is_settled_before_injection():
    world = _build()
    assert all(b.status == BatchStatus.SETTLED for b in world.batches)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_seed_produces_identical_world():
    a = _build(seed=7)
    b = _build(seed=7)
    assert [p.model_dump(mode="json") for p in a.payments] == [p.model_dump(mode="json") for p in b.payments]
    assert [r.model_dump(mode="json") for r in a.refunds] == [r.model_dump(mode="json") for r in b.refunds]
    assert [c.model_dump(mode="json") for c in a.bank_credits] == [c.model_dump(mode="json") for c in b.bank_credits]


def test_different_seeds_produce_different_worlds():
    a = _build(seed=1)
    b = _build(seed=2)
    assert [p.amount.paise for p in a.payments] != [p.amount.paise for p in b.payments]


# ---------------------------------------------------------------------------
# Payment sampling shape
# ---------------------------------------------------------------------------


def test_payment_count_matches_config():
    world = _build()
    assert len(world.payments) == _SMALL_CONFIG.total_payments


def test_every_payment_amount_within_floor_and_cap():
    world = _build()
    for p in world.payments:
        assert _SMALL_CONFIG.amount_floor_paise <= p.amount.paise <= _SMALL_CONFIG.amount_cap_paise


def test_method_mix_within_statistical_tolerance():
    world = _build(config=GenerationConfig(month="2026-07", total_payments=6_000))
    n = len(world.payments)
    counts = defaultdict(int)
    for p in world.payments:
        counts[p.method] += 1
    for method, weight in _SMALL_CONFIG.method_weights.items():
        assert counts[method] / n == pytest.approx(weight, abs=0.03)


def test_every_payment_in_true_world_has_a_settlement_id():
    # D08 (missing transaction) only exists after injection -- the true
    # world always fully settles every payment.
    world = _build()
    assert all(p.settlement_id is not None for p in world.payments)


def test_no_zero_value_fee_lines_emitted():
    world = _build()
    assert all(f.computed_amount.paise > 0 for f in world.fee_lines)


def test_upi_payments_have_a_fixed_fee_line_but_no_mdr_line():
    from core.models import FeeType, PaymentMethod

    world = _build(config=GenerationConfig(month="2026-07", total_payments=2_000))
    upi_payment_ids = {p.id for p in world.payments if p.method == PaymentMethod.UPI}
    upi_fee_lines = [f for f in world.fee_lines if f.applies_to_id in upi_payment_ids]
    assert upi_fee_lines, "expected at least one UPI payment with a fixed fee line"
    assert all(f.fee_type == FeeType.FIXED for f in upi_fee_lines)


# ---------------------------------------------------------------------------
# Refunds: cross-cycle behaviour is the case the spec calls out by name
# ---------------------------------------------------------------------------


def test_refund_rate_is_approximately_configured():
    world = _build(config=GenerationConfig(month="2026-07", total_payments=6_000))
    assert len(world.refunds) / len(world.payments) == pytest.approx(_SMALL_CONFIG.refund_rate, abs=0.015)


def test_meaningful_share_of_refunds_land_in_a_different_cycle_than_their_payment():
    world = _build(config=GenerationConfig(month="2026-07", total_payments=6_000))
    payment_settlement = {p.id: p.settlement_id for p in world.payments}
    cross_cycle = [r for r in world.refunds if payment_settlement[r.payment_id] != r.settlement_id]
    assert len(cross_cycle) / len(world.refunds) > 0.5


def test_some_refunds_are_partial_and_some_are_full():
    world = _build(config=GenerationConfig(month="2026-07", total_payments=6_000))
    assert any(r.is_partial for r in world.refunds)
    assert any(not r.is_partial for r in world.refunds)


def test_partial_refund_amount_is_less_than_payment_amount():
    world = _build(config=GenerationConfig(month="2026-07", total_payments=6_000))
    payment_by_id = {p.id: p for p in world.payments}
    for r in world.refunds:
        if r.is_partial:
            assert r.amount.paise < payment_by_id[r.payment_id].amount.paise


# ---------------------------------------------------------------------------
# Chargebacks: won ones get a reversal Adjustment; lost ones don't
# ---------------------------------------------------------------------------


def test_won_chargeback_has_a_matching_reversal_adjustment():
    world = _build(config=GenerationConfig(month="2026-07", total_payments=8_000))
    won = [c for c in world.chargebacks if c.stage == ChargebackStage.WON]
    assert won, "expected at least one won chargeback at this scale"
    reversal_reasons = {a.reason for a in world.adjustments if a.kind == AdjustmentKind.MANUAL_CREDIT}
    for cb in won:
        assert any(cb.id in reason for reason in reversal_reasons), f"no reversal adjustment found for won chargeback {cb.id}"


def test_lost_chargeback_has_no_reversal_adjustment():
    world = _build(config=GenerationConfig(month="2026-07", total_payments=8_000))
    lost = [c for c in world.chargebacks if c.stage == ChargebackStage.LOST]
    assert lost, "expected at least one lost chargeback at this scale"
    reversal_reasons = {a.reason for a in world.adjustments if a.kind == AdjustmentKind.MANUAL_CREDIT}
    for cb in lost:
        assert not any(cb.id in reason for reason in reversal_reasons)


# ---------------------------------------------------------------------------
# Reserve holds/releases
# ---------------------------------------------------------------------------


def test_reserve_hold_exists_for_every_batch_with_payments():
    world = _build()
    batches_with_payments = {p.settlement_id for p in world.payments}
    hold_batches = {a.settlement_id for a in world.adjustments if a.kind == AdjustmentKind.RESERVE_HOLD}
    assert batches_with_payments <= hold_batches


def test_late_month_reserve_holds_have_no_release_within_the_window():
    world = _build()
    from datagen.timeline import month_bounds

    _, last_day = month_bounds(_SMALL_CONFIG.month)
    releases = [a for a in world.adjustments if a.kind == AdjustmentKind.RESERVE_RELEASE]
    assert all(a.created_at.date() <= last_day for a in releases)


# ---------------------------------------------------------------------------
# Fee-line / tax-line record shape (RecordRef-compatible)
# ---------------------------------------------------------------------------


def test_fee_lines_apply_to_payments():
    world = _build()
    for f in world.fee_lines:
        assert f.applies_to_type == EntityType.PAYMENT
