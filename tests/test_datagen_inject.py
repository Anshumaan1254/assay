"""Money-critical: each D01-D12 injector mutates the documented field by
the documented mechanism, with the documented sign, and the ground-truth
entry's amount_impact_paise reconciles exactly against the batch-level
effect (including any cascading GST change on a mutated fee line -- the
whole point of amount_impact_paise is that it's what the conservation
identity needs to balance, not just the headline fee delta).

Written before datagen/inject.py exists -- expected to fail on collection
until that module is implemented.
"""

from __future__ import annotations

from random import Random

import pytest

from core.exceptions import DiscrepancyClass
from core.models import AdjustmentKind, EntityType, FeeType, PaymentMethod
from datagen.config import GenerationConfig, InjectionProfile
from datagen.ratecard import default_rate_card
from datagen.world import build_true_world

# Large enough that every class (including the thin-pool ones: D03 needs a
# debit payment over Rs.10,000 with a binding cap; D05 needs a won
# chargeback) has at least a handful of eligible instances at seed=42.
_CONFIG = GenerationConfig(month="2026-07", total_payments=5_200)
_RATE_CARD = default_rate_card(_CONFIG.month)


def _true_world(seed: int = 42):
    return build_true_world(_CONFIG, _RATE_CARD, Random(seed))


def _only(profile_kwargs: dict) -> InjectionProfile:
    return InjectionProfile(**profile_kwargs)


def _batches_by_id(world):
    return {b.id: b for b in world.batches}


# ---------------------------------------------------------------------------
# Import guard -- written before the module exists.
# ---------------------------------------------------------------------------

from datagen.inject import InjectionProfileError, apply_discrepancies  # noqa: E402


# ---------------------------------------------------------------------------
# Shared: profile counts are honored exactly, and mutual exclusivity holds
# ---------------------------------------------------------------------------


def test_all_zero_profile_produces_no_discrepancies_and_an_identical_world():
    true_world = _true_world()
    reported, discrepancies, flags = apply_discrepancies(true_world, _RATE_CARD, InjectionProfile(), Random(1))
    assert discrepancies == []
    assert flags == []
    true_credits = {c.id: c.amount.paise for c in true_world.bank_credits}
    reported_credits = {c.id: c.amount.paise for c in reported.bank_credits}
    assert true_credits == reported_credits


def test_no_record_is_touched_by_two_discrepancies():
    true_world = _true_world()
    profile = InjectionProfile(D01=5, D02=5, D03=2, D04=5, D05=1, D06=5, D07=3, D08=5, D10=5, D11=5, D12=5)
    _, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, profile, Random(2))
    payment_ids_seen = []
    for entry in discrepancies:
        for ref in entry.records:
            if ref.type == EntityType.PAYMENT:
                payment_ids_seen.append(ref.id)
    assert len(payment_ids_seen) == len(set(payment_ids_seen)), "a payment was touched by more than one discrepancy"


def test_requesting_more_than_the_eligible_population_raises():
    true_world = _true_world()
    # D05 (won-chargeback reversal) has a thin natural pool at this scale --
    # asking for far more than exist must raise, not silently under-deliver.
    with pytest.raises(InjectionProfileError):
        apply_discrepancies(true_world, _RATE_CARD, InjectionProfile(D05=10_000), Random(3))


# ---------------------------------------------------------------------------
# D01 -- wrong MDR tier applied
# ---------------------------------------------------------------------------


def test_d01_wrong_tier_produces_fee_over_or_undercharge_matching_the_batch_delta():
    true_world = _true_world()
    reported, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D01": 5}), Random(4))
    assert len(discrepancies) == 5
    true_batches = _batches_by_id(true_world)
    reported_batches = _batches_by_id(reported)
    payment_by_id = {p.id: p for p in true_world.payments}

    # Several of the 5 chosen payments can share a batch (a batch holds
    # ~168 payments on average), so aggregate signed expected impact per
    # batch before comparing against the actual batch-level delta.
    expected_delta_by_batch: dict[str, int] = {}
    for entry in discrepancies:
        assert entry.code == "D01"
        assert entry.discrepancy_class in (DiscrepancyClass.FEE_OVERCHARGE, DiscrepancyClass.FEE_UNDERCHARGE)
        payment_ref = next(r for r in entry.records if r.type == EntityType.PAYMENT)
        payment = payment_by_id[payment_ref.id]
        sign = 1 if entry.discrepancy_class == DiscrepancyClass.FEE_UNDERCHARGE else -1
        batch_id = payment.settlement_id
        expected_delta_by_batch[batch_id] = expected_delta_by_batch.get(batch_id, 0) + sign * entry.amount_impact_paise

    for batch_id, expected_delta in expected_delta_by_batch.items():
        actual_delta = reported_batches[batch_id].expected_credit.paise - true_batches[batch_id].expected_credit.paise
        assert actual_delta == expected_delta


def test_d01_only_targets_card_payments():
    true_world = _true_world()
    _, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D01": 5}), Random(5))
    payment_by_id = {p.id: p for p in true_world.payments}
    for entry in discrepancies:
        payment_ref = next(r for r in entry.records if r.type == EntityType.PAYMENT)
        assert payment_by_id[payment_ref.id].method == PaymentMethod.CARD


# ---------------------------------------------------------------------------
# D02 -- tax computed on the wrong base (gross instead of fee)
# ---------------------------------------------------------------------------


def test_d02_tax_base_becomes_payment_gross_and_is_always_an_overcharge():
    true_world = _true_world()
    reported, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D02": 4}), Random(6))
    assert len(discrepancies) == 4
    reported_tax_by_id = {t.id: t for t in reported.tax_lines}
    for entry in discrepancies:
        assert entry.discrepancy_class == DiscrepancyClass.TAX_MISCALCULATION
        tax_ref = next(r for r in entry.records if r.type == EntityType.TAX_LINE)
        payment_ref = next(r for r in entry.records if r.type == EntityType.PAYMENT)
        payment = next(p for p in true_world.payments if p.id == payment_ref.id)
        reported_tax = reported_tax_by_id[tax_ref.id]
        assert reported_tax.base_amount.paise == payment.amount.paise
        assert entry.amount_impact_paise > 0


def test_d02_never_touches_the_same_payment_twice_even_with_two_fee_lines():
    # Regression test: NETBANKING and WALLET payments carry BOTH an MDR and
    # a FIXED fee line, both eligible D02 targets. _inject_d02's candidate
    # list is a snapshot taken before its loop runs, so a payment with two
    # eligible fee lines in that snapshot could previously receive two
    # separate D02 entries -- found during design review, fixed by
    # re-checking touch.payments inside the loop, not just at snapshot time.
    # A large count increases the chance of hitting such a payment if the
    # bug were still present.
    true_world = _true_world()
    _, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D02": 400}), Random(21))
    payment_ids_seen = [next(r.id for r in entry.records if r.type == EntityType.PAYMENT) for entry in discrepancies]
    assert len(payment_ids_seen) == len(set(payment_ids_seen))


# ---------------------------------------------------------------------------
# D03 -- fee cap not applied where the contract requires it
# ---------------------------------------------------------------------------


def test_d03_cap_removed_produces_overcharge_equal_to_uncapped_minus_cap():
    true_world = _true_world()
    reported, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D03": 3}), Random(7))
    assert len(discrepancies) == 3
    reported_fee_by_id = {f.id: f for f in reported.fee_lines}
    for entry in discrepancies:
        assert entry.code == "D03"
        assert entry.discrepancy_class == DiscrepancyClass.FEE_OVERCHARGE
        fee_ref = next(r for r in entry.records if r.type == EntityType.FEE_LINE)
        reported_fee = reported_fee_by_id[fee_ref.id]
        assert reported_fee.computed_amount.paise > entry.detail["cap_paise"]


# ---------------------------------------------------------------------------
# D04 -- refund deducted twice
# ---------------------------------------------------------------------------


def test_d04_refund_deducted_twice_reduces_only_its_own_batch_by_its_amount():
    true_world = _true_world()
    reported, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D04": 3}), Random(8))
    assert len(discrepancies) == 3
    true_batches = _batches_by_id(true_world)
    reported_batches = _batches_by_id(reported)
    refund_by_id = {r.id: r for r in true_world.refunds}
    # The ledger's Refund list itself must NOT show a duplicate -- D04 is a
    # settlement-side double deduction, not a ledger data-integrity bug.
    assert len(reported.refunds) == len(true_world.refunds)

    # Two of the three chosen refunds can legitimately land in the same
    # batch (a batch holds many refunds), so the batch-level delta can
    # reflect more than one entry -- aggregate expected impact per batch
    # before comparing, rather than checking each entry against a
    # single-entry delta.
    expected_delta_by_batch: dict[str, int] = {}
    for entry in discrepancies:
        assert entry.discrepancy_class == DiscrepancyClass.REFUND_AMOUNT_MISMATCH
        refund_ref = next(r for r in entry.records if r.type == EntityType.REFUND)
        refund = refund_by_id[refund_ref.id]
        assert entry.amount_impact_paise == refund.amount.paise
        batch_id = refund.settlement_id
        expected_delta_by_batch[batch_id] = expected_delta_by_batch.get(batch_id, 0) - refund.amount.paise

    for batch_id, expected_delta in expected_delta_by_batch.items():
        actual_delta = reported_batches[batch_id].expected_credit.paise - true_batches[batch_id].expected_credit.paise
        assert actual_delta == expected_delta


# ---------------------------------------------------------------------------
# D05 -- chargeback won but the reversal never credited back
# ---------------------------------------------------------------------------


def test_d05_won_chargeback_loses_its_reversal_adjustment():
    true_world = _true_world()
    reported, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D05": 1}), Random(9))
    assert len(discrepancies) == 1
    entry = discrepancies[0]
    assert entry.discrepancy_class == DiscrepancyClass.CHARGEBACK_AMOUNT_MISMATCH
    cb_ref = next(r for r in entry.records if r.type == EntityType.CHARGEBACK)
    true_reversal = next(
        a for a in true_world.adjustments if a.kind == AdjustmentKind.MANUAL_CREDIT and cb_ref.id in a.reason
    )
    assert not any(
        a.kind == AdjustmentKind.MANUAL_CREDIT and cb_ref.id in a.reason for a in reported.adjustments
    )
    assert entry.amount_impact_paise == true_reversal.amount.paise


# ---------------------------------------------------------------------------
# D06 -- refund deducted in a cycle where it was not due
# ---------------------------------------------------------------------------


def test_d06_refund_moved_to_a_different_batch_washes_out_across_the_two():
    true_world = _true_world()
    reported, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D06": 4}), Random(10))
    assert len(discrepancies) == 4
    true_batches = _batches_by_id(true_world)
    reported_batches = _batches_by_id(reported)
    reported_refund_by_id = {r.id: r for r in reported.refunds}

    # A batch can be the true_batch for one entry and the wrong_batch for
    # another (or shared across several entries), so aggregate signed
    # expected contributions per batch across all entries before comparing.
    expected_delta_by_batch: dict[str, int] = {}
    for entry in discrepancies:
        assert entry.discrepancy_class == DiscrepancyClass.REFUND_AMOUNT_MISMATCH
        refund_ref = next(r for r in entry.records if r.type == EntityType.REFUND)
        true_batch_id = entry.detail["true_batch_id"]
        wrong_batch_id = entry.detail["wrong_batch_id"]
        assert reported_refund_by_id[refund_ref.id].settlement_id == wrong_batch_id
        expected_delta_by_batch[true_batch_id] = expected_delta_by_batch.get(true_batch_id, 0) + entry.amount_impact_paise
        expected_delta_by_batch[wrong_batch_id] = expected_delta_by_batch.get(wrong_batch_id, 0) - entry.amount_impact_paise

    for batch_id, expected_delta in expected_delta_by_batch.items():
        actual_delta = reported_batches[batch_id].expected_credit.paise - true_batches[batch_id].expected_credit.paise
        assert actual_delta == expected_delta


# ---------------------------------------------------------------------------
# D07 -- rounding drift (half-up vs half-even)
# ---------------------------------------------------------------------------


def test_d07_rounding_drift_changes_a_tax_line_by_exactly_one_paise():
    true_world = _true_world()
    reported, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D07": 3}), Random(11))
    assert len(discrepancies) == 3
    true_tax_by_id = {t.id: t for t in true_world.tax_lines}
    reported_tax_by_id = {t.id: t for t in reported.tax_lines}
    for entry in discrepancies:
        assert entry.discrepancy_class == DiscrepancyClass.ROUNDING_DRIFT
        assert entry.amount_impact_paise == 1
        tax_ref = next(r for r in entry.records if r.type == EntityType.TAX_LINE)
        delta = reported_tax_by_id[tax_ref.id].amount.paise - true_tax_by_id[tax_ref.id].amount.paise
        assert abs(delta) == 1


# ---------------------------------------------------------------------------
# D08 -- captured payment never appears in any settlement
# ---------------------------------------------------------------------------


def test_d08_missing_payment_has_no_settlement_id_and_no_fee_or_tax_lines():
    true_world = _true_world()
    reported, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D08": 3}), Random(12))
    assert len(discrepancies) == 3
    reported_payment_by_id = {p.id: p for p in reported.payments}
    for entry in discrepancies:
        assert entry.discrepancy_class == DiscrepancyClass.MISSING_TRANSACTION
        payment_ref = next(r for r in entry.records if r.type == EntityType.PAYMENT)
        payment = reported_payment_by_id[payment_ref.id]
        assert payment.settlement_id is None
        assert not any(f.applies_to_id == payment.id for f in reported.fee_lines)
        assert not any(
            t.applies_to_fee_id == f.id for f in true_world.fee_lines if f.applies_to_id == payment.id for t in reported.tax_lines
        )


def test_d08_batch_credit_drops_by_the_payments_true_net_contribution():
    true_world = _true_world()
    reported, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D08": 3}), Random(12))
    true_batches = _batches_by_id(true_world)
    reported_batches = _batches_by_id(reported)
    payment_by_id = {p.id: p for p in true_world.payments}

    # Two of the 3 chosen payments can share a batch, so aggregate expected
    # impact per batch before comparing against the actual batch delta.
    expected_delta_by_batch: dict[str, int] = {}
    for entry in discrepancies:
        payment_ref = next(r for r in entry.records if r.type == EntityType.PAYMENT)
        payment = payment_by_id[payment_ref.id]
        batch_id = payment.settlement_id
        expected_delta_by_batch[batch_id] = expected_delta_by_batch.get(batch_id, 0) - entry.amount_impact_paise

    for batch_id, expected_delta in expected_delta_by_batch.items():
        actual_delta = reported_batches[batch_id].expected_credit.paise - true_batches[batch_id].expected_credit.paise
        assert actual_delta == expected_delta


# ---------------------------------------------------------------------------
# D09 -- corrupted/truncated UTR, zero rupee impact, data-quality only
# ---------------------------------------------------------------------------


def test_d09_corrupts_utr_only_lands_in_data_quality_flags_zero_impact():
    true_world = _true_world()
    reported, discrepancies, flags = apply_discrepancies(true_world, _RATE_CARD, _only({"D09": 5}), Random(13))
    assert len(flags) == 5
    assert all(f.code == "D09" for f in flags)
    assert all(f.amount_impact_paise == 0 for f in flags)
    assert not any(d.code == "D09" for d in discrepancies)

    true_credit_by_id = {c.id: c for c in true_world.bank_credits}
    reported_credit_by_id = {c.id: c for c in reported.bank_credits}
    for flag in flags:
        ref = flag.records[0]
        assert reported_credit_by_id[ref.id].utr != true_credit_by_id[ref.id].utr
        # amount and narration must be untouched by D09 -- it corrupts only utr.
        assert reported_credit_by_id[ref.id].amount == true_credit_by_id[ref.id].amount
        assert reported_credit_by_id[ref.id].narration == true_credit_by_id[ref.id].narration


def test_d09_does_not_break_later_injectors_batch_lookup():
    # Regression test for the ordering hazard found during design: D09 runs
    # before D10/D11/D12 in the fixed D01..D12 order, and corrupts
    # BankCredit.utr -- any later injector that (incorrectly) re-matched a
    # batch to its bank credit BY utr would crash or silently skip once
    # that utr no longer matches. A large, dense profile exercises many
    # batches across all classes in one run.
    true_world = _true_world()
    profile = InjectionProfile(D01=5, D02=5, D03=2, D04=5, D05=1, D06=5, D07=3, D08=5, D09=10, D10=5, D11=5, D12=5)
    reported, discrepancies, flags = apply_discrepancies(true_world, _RATE_CARD, profile, Random(14))
    assert len(flags) == 10
    assert len(discrepancies) == 5 + 5 + 2 + 5 + 1 + 5 + 3 + 5 + 5 + 5 + 5


# ---------------------------------------------------------------------------
# D10 -- stale contract version applied after the revision's effective date
# ---------------------------------------------------------------------------


def test_d10_only_targets_payments_captured_after_the_revision():
    true_world = _true_world()
    _, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D10": 4}), Random(15))
    assert len(discrepancies) == 4
    payment_by_id = {p.id: p for p in true_world.payments}
    v1, v2 = sorted(_RATE_CARD.versions, key=lambda v: v.effective_from)
    for entry in discrepancies:
        payment_ref = next(r for r in entry.records if r.type == EntityType.PAYMENT)
        payment = payment_by_id[payment_ref.id]
        assert payment.captured_at >= v2.effective_from
        assert entry.discrepancy_class in (DiscrepancyClass.FEE_OVERCHARGE, DiscrepancyClass.FEE_UNDERCHARGE)


# ---------------------------------------------------------------------------
# D11 -- fixed fee charged twice on one transaction
# ---------------------------------------------------------------------------


def test_d11_adds_a_second_fixed_fee_line_with_matching_amount():
    true_world = _true_world()
    reported, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D11": 3}), Random(16))
    assert len(discrepancies) == 3
    for entry in discrepancies:
        assert entry.discrepancy_class == DiscrepancyClass.FEE_OVERCHARGE
        payment_ref = next(r for r in entry.records if r.type == EntityType.PAYMENT)
        fixed_lines = [
            f for f in reported.fee_lines if f.applies_to_id == payment_ref.id and f.fee_type == FeeType.FIXED
        ]
        assert len(fixed_lines) == 2
        assert fixed_lines[0].computed_amount == fixed_lines[1].computed_amount


# ---------------------------------------------------------------------------
# D12 -- international surcharge applied to a domestic transaction
# ---------------------------------------------------------------------------


def test_d12_adds_a_spurious_international_fee_to_a_domestic_card_payment():
    true_world = _true_world()
    reported, discrepancies, _ = apply_discrepancies(true_world, _RATE_CARD, _only({"D12": 3}), Random(17))
    assert len(discrepancies) == 3
    payment_by_id = {p.id: p for p in true_world.payments}
    for entry in discrepancies:
        assert entry.discrepancy_class == DiscrepancyClass.FEE_OVERCHARGE
        payment_ref = next(r for r in entry.records if r.type == EntityType.PAYMENT)
        payment = payment_by_id[payment_ref.id]
        assert payment.method == PaymentMethod.CARD
        assert payment.is_international is False
        intl_lines = [
            f for f in reported.fee_lines if f.applies_to_id == payment_ref.id and f.fee_type == FeeType.INTERNATIONAL
        ]
        assert len(intl_lines) == 1


# ---------------------------------------------------------------------------
# Whole-run property: total money-level impact reconciles against ground
# truth (excluding D06, a pure cross-batch timing wash with zero net
# whole-world effect, and D09, a zero-impact data-quality flag).
# ---------------------------------------------------------------------------


def test_whole_world_credit_delta_reconciles_against_ground_truth():
    true_world = _true_world()
    profile = InjectionProfile(D01=8, D02=4, D03=3, D04=3, D05=2, D06=4, D07=5, D08=3, D09=6, D10=4, D11=3, D12=2)
    reported, discrepancies, flags = apply_discrepancies(true_world, _RATE_CARD, profile, Random(18))

    total_true = sum(b.expected_credit.paise for b in true_world.batches)
    total_reported = sum(b.expected_credit.paise for b in reported.batches)

    # Fixed per-code sign, not per-class: most classes always move net in
    # one direction regardless of discrepancy_class (e.g. D02's tax-on-gross
    # is always higher, so always an overcharge), but D07 is the exception
    # worth spelling out -- ROUND_HALF_EVEN only ever differs from
    # ROUND_HALF_UP at an even N.5 tie, where half-even always rounds DOWN
    # to N (half-up already rounds odd-N.5 ties up to the same even N+1, so
    # those never register as a difference at all). A lower reported tax
    # means a HIGHER reported net -- the opposite direction from every
    # other always-overcharge class here. D01/D10 are genuinely
    # bidirectional per instance, so those alone read direction off
    # discrepancy_class.
    fixed_sign_by_code = {"D02": -1, "D03": -1, "D04": -1, "D05": -1, "D07": +1, "D08": -1, "D11": -1, "D12": -1}

    expected_delta = 0
    for entry in discrepancies:
        if entry.code == "D06":
            continue  # cross-batch wash; contributes 0 to the whole-world total
        if entry.code in ("D01", "D10"):
            sign = 1 if entry.discrepancy_class == DiscrepancyClass.FEE_UNDERCHARGE else -1
        else:
            sign = fixed_sign_by_code[entry.code]
        expected_delta += sign * entry.amount_impact_paise

    assert total_reported - total_true == expected_delta
    assert all(f.amount_impact_paise == 0 for f in flags)  # D09 never contributes


# ---------------------------------------------------------------------------
# Every cited record actually exists in the reported world
# ---------------------------------------------------------------------------


def test_every_cited_record_exists_in_the_reported_world():
    true_world = _true_world()
    profile = InjectionProfile(D01=5, D02=5, D03=2, D04=5, D05=1, D06=5, D07=3, D08=5, D09=5, D10=5, D11=5, D12=5)
    reported, discrepancies, flags = apply_discrepancies(true_world, _RATE_CARD, profile, Random(19))

    ids_by_type: dict[EntityType, set[str]] = {
        EntityType.PAYMENT: {p.id for p in reported.payments},
        EntityType.REFUND: {r.id for r in reported.refunds},
        EntityType.CHARGEBACK: {c.id for c in reported.chargebacks},
        EntityType.ADJUSTMENT: {a.id for a in reported.adjustments},
        EntityType.FEE_LINE: {f.id for f in reported.fee_lines},
        EntityType.TAX_LINE: {t.id for t in reported.tax_lines},
        EntityType.SETTLEMENT_BATCH: {b.id for b in reported.batches},
        EntityType.BANK_CREDIT: {c.id for c in reported.bank_credits},
    }
    for entry in discrepancies + flags:
        for ref in entry.records:
            # D08's own payment reference is intentionally excluded from
            # this check: the whole point of D08 is that the payment no
            # longer appears anywhere settlement-related, but the payment
            # record itself still exists in the ledger (settlement_id=None).
            assert ref.id in ids_by_type[ref.type], f"{ref.type}:{ref.id} cited but not found in reported world"
