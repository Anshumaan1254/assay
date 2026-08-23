"""Money-critical: silent-corruption mode flips exactly one paise on
exactly one record, deterministically, and never produces a negative
amount. This is the demo scenario proving invariant 3's zero-tolerance
conservation check catches even a single paise -- it deliberately does
NOT recompute any batch/bank-credit total afterward (see
datagen/inject.py::apply_silent_corruption's docstring): the whole point
is an uncompensated discrepancy only an independent recomputation can
catch, not one the generator quietly balances back out.
"""

from __future__ import annotations

from random import Random

from core.money import Money
from datagen.config import GenerationConfig
from datagen.inject import apply_silent_corruption
from datagen.ratecard import default_rate_card
from datagen.world import build_true_world

_CONFIG = GenerationConfig(month="2026-07", total_payments=600)
_RATE_CARD = default_rate_card(_CONFIG.month)


def _true_world(seed: int = 42):
    return build_true_world(_CONFIG, _RATE_CARD, Random(seed))


def _get_field(world, record_type: str, record_id: str, field: str) -> Money:
    attr_by_type = {
        "payment": "payments",
        "refund": "refunds",
        "chargeback": "chargebacks",
        "adjustment": "adjustments",
        "fee_line": "fee_lines",
        "tax_line": "tax_lines",
        "settlement_batch": "batches",
        "bank_credit": "bank_credits",
    }
    items = getattr(world, attr_by_type[record_type])
    record = next(x for x in items if x.id == record_id)
    return getattr(record, field)


def test_flips_exactly_one_paise():
    world = _true_world()
    _, corruption = apply_silent_corruption(world, Random(1))
    assert abs(corruption.delta_paise) == 1
    assert corruption.corrupted_paise - corruption.original_paise == corruption.delta_paise


def test_never_produces_a_negative_amount():
    world = _true_world()
    for seed in range(50):
        _, corruption = apply_silent_corruption(world, Random(seed))
        assert corruption.corrupted_paise >= 0


def test_deterministic_with_seed():
    world = _true_world()
    _, a = apply_silent_corruption(world, Random(55))
    _, b = apply_silent_corruption(world, Random(55))
    assert a == b


def test_different_seeds_can_pick_different_records():
    world = _true_world()
    picks = {apply_silent_corruption(world, Random(s))[1].record_id for s in range(30)}
    assert len(picks) > 1


def test_touches_only_the_one_declared_field_on_the_one_declared_record():
    world = _true_world()
    reported, corruption = apply_silent_corruption(world, Random(2))

    corrupted_value = _get_field(reported, corruption.record_type, corruption.record_id, corruption.field)
    assert corrupted_value.paise == corruption.corrupted_paise

    # Every OTHER record of the same type must be byte-identical to the
    # true world -- this is a single-record, single-field corruption, not
    # a systemic shift.
    attr_by_type = {
        "payment": "payments",
        "refund": "refunds",
        "chargeback": "chargebacks",
        "adjustment": "adjustments",
        "fee_line": "fee_lines",
        "tax_line": "tax_lines",
        "settlement_batch": "batches",
        "bank_credit": "bank_credits",
    }
    attr = attr_by_type[corruption.record_type]
    true_by_id = {r.id: r for r in getattr(world, attr)}
    reported_by_id = {r.id: r for r in getattr(reported, attr)}
    for record_id, true_record in true_by_id.items():
        if record_id == corruption.record_id:
            continue
        assert reported_by_id[record_id] == true_record


def test_does_not_recompute_any_batch_or_bank_credit_total():
    # Deliberate: silent corruption must NOT self-heal by adjusting the
    # batch/bank-credit total to match -- that would make the discrepancy
    # invisible to a conservation check, defeating the entire point.
    world = _true_world()
    reported, corruption = apply_silent_corruption(world, Random(3))
    true_batches = {b.id: b.expected_credit.paise for b in world.batches}
    reported_batches = {b.id: b.expected_credit.paise for b in reported.batches}
    true_credits = {c.id: c.amount.paise for c in world.bank_credits}
    reported_credits = {c.id: c.amount.paise for c in reported.bank_credits}

    if corruption.record_type not in ("settlement_batch", "bank_credit"):
        # The corrupted record is a line item feeding a batch total; that
        # batch's own expected_credit/bank_credit must stay exactly as it
        # was computed for the true world -- unrecomputed, unreconciled.
        assert true_batches == reported_batches
        assert true_credits == reported_credits
    else:
        # The corrupted record IS the batch or bank credit itself: exactly
        # that one entry differs, nothing else.
        diffs_batches = {k for k in true_batches if true_batches[k] != reported_batches[k]}
        diffs_credits = {k for k in true_credits if true_credits[k] != reported_credits[k]}
        assert len(diffs_batches) + len(diffs_credits) == 1
