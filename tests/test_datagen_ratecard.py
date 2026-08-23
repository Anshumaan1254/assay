"""Money-critical: tier lookup, cap enforcement, GST base, TDS arithmetic,
effective-dated version selection, and rounding-mode sensitivity for
datagen's own rate-card calculator (independent of core/contract.py, which
is still a stub).

Written before datagen/ratecard.py exists — expected to fail on collection
until that module is implemented.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP

import pytest

from core.models import IST, CardType, PaymentMethod
from datagen.ratecard import (
    MDRTier,
    MethodFeeRule,
    RateCard,
    RateCardVersion,
    TDSClause,
    compute_tds,
    default_rate_card,
    gst_amount,
    mdr_amount,
    mdr_raw_amount,
    render_markdown,
)


def _dt(y, m, d, hh=0, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


# ---------------------------------------------------------------------------
# mdr_raw_amount / mdr_amount — bps arithmetic and cap enforcement
# ---------------------------------------------------------------------------


def test_mdr_raw_amount_exact_bps_no_rounding_needed():
    # ₹5,000 (500000 paise) at 160 bps (1.60%) = exactly 8000 paise, no
    # rounding ambiguity.
    assert mdr_raw_amount(500_000, 160) == 8_000


def test_mdr_raw_amount_round_half_up_vs_half_even_disagree():
    # 25 paise * 200 bps / 10000 = 0.5 paise exactly — the canonical case
    # where the two rounding modes diverge, deliberately isolated from the
    # real tier table's numbers so this test is about rounding, not tiers.
    assert mdr_raw_amount(25, 200, rounding=ROUND_HALF_UP) == 1
    assert mdr_raw_amount(25, 200, rounding=ROUND_HALF_EVEN) == 0


def test_mdr_amount_applies_cap_when_set():
    tier = MDRTier(min_paise=1_000_000, max_paise=None, bps=70, cap_paise=15_000)
    # ₹50,000 gross at 70 bps = 35000 paise uncapped; capped tier caps at 15000.
    assert mdr_amount(5_000_000, tier) == 15_000


def test_mdr_amount_does_not_cap_when_under_cap():
    tier = MDRTier(min_paise=1_000_000, max_paise=None, bps=70, cap_paise=15_000)
    # ₹15,000 gross at 70 bps = 10500 paise, under the 15000 cap.
    assert mdr_amount(1_500_000, tier) == 10_500


def test_mdr_amount_uncapped_tier_is_unaffected():
    tier = MDRTier(min_paise=0, max_paise=200_000, bps=180, cap_paise=None)
    assert mdr_amount(150_000, tier) == mdr_raw_amount(150_000, 180)


# ---------------------------------------------------------------------------
# MethodFeeRule.tier_for — half-open [min, max) boundaries
# ---------------------------------------------------------------------------


def _credit_rule() -> MethodFeeRule:
    return MethodFeeRule(
        method=PaymentMethod.CARD,
        card_type=CardType.CREDIT,
        tiers=(
            MDRTier(min_paise=0, max_paise=200_000, bps=180),
            MDRTier(min_paise=200_000, max_paise=1_000_000, bps=160),
            MDRTier(min_paise=1_000_000, max_paise=None, bps=140),
        ),
        fixed_fee_paise=0,
        international_surcharge_bps=200,
    )


def test_tier_for_below_first_boundary():
    assert _credit_rule().tier_for(199_999).bps == 180


def test_tier_for_at_first_boundary_is_exclusive_of_lower_tier():
    # min_paise inclusive / max_paise exclusive: exactly 200000 belongs to
    # the tier that STARTS at 200000, not the one that ends there.
    assert _credit_rule().tier_for(200_000).bps == 160


def test_tier_for_at_second_boundary():
    assert _credit_rule().tier_for(1_000_000).bps == 140


def test_tier_for_far_above_last_boundary_uses_unbounded_tier():
    assert _credit_rule().tier_for(50_000_000).bps == 140


def test_tier_for_no_match_raises():
    rule = MethodFeeRule(
        method=PaymentMethod.CARD,
        card_type=CardType.DEBIT,
        tiers=(MDRTier(min_paise=100, max_paise=200, bps=90),),
        fixed_fee_paise=0,
    )
    with pytest.raises(ValueError):
        rule.tier_for(0)


# ---------------------------------------------------------------------------
# RateCardVersion.rule_for
# ---------------------------------------------------------------------------


def test_rule_for_unknown_method_card_type_raises():
    version = RateCardVersion(
        version_id="v-test",
        effective_from=_dt(2026, 7, 1),
        gst_bps=1800,
        fee_rules=(_credit_rule(),),
        tds=TDSClause(rate_bps=100, active=False, applies_to="marketplace transactions"),
    )
    with pytest.raises(KeyError):
        version.rule_for(PaymentMethod.CARD, CardType.PREPAID)


def test_rule_for_finds_matching_method_and_card_type():
    version = RateCardVersion(
        version_id="v-test",
        effective_from=_dt(2026, 7, 1),
        gst_bps=1800,
        fee_rules=(_credit_rule(),),
        tds=TDSClause(rate_bps=100, active=False, applies_to="marketplace transactions"),
    )
    found = version.rule_for(PaymentMethod.CARD, CardType.CREDIT)
    assert found.method == PaymentMethod.CARD
    assert found.card_type == CardType.CREDIT
    assert found == _credit_rule()


# ---------------------------------------------------------------------------
# gst_amount — applied to the fee amount, not gross; rounding-sensitive too
# ---------------------------------------------------------------------------


def test_gst_amount_on_fee_base():
    # 18% GST on a 918-paise fee = 165.24 -> rounds to 165 (half-up).
    assert gst_amount(918, 1800) == 165


def test_gst_amount_round_half_up_vs_half_even_disagree():
    assert gst_amount(25, 200, rounding=ROUND_HALF_UP) == 1
    assert gst_amount(25, 200, rounding=ROUND_HALF_EVEN) == 0


# ---------------------------------------------------------------------------
# compute_tds — pure arithmetic, independent of the clause's active flag
# ---------------------------------------------------------------------------


def test_compute_tds_one_percent_of_gross():
    clause = TDSClause(rate_bps=100, active=True, applies_to="marketplace transactions")
    assert compute_tds(1_000_000, clause) == 10_000  # 1% of 10000 rupees = 100 rupees


def test_compute_tds_arithmetic_ignores_active_flag():
    active = TDSClause(rate_bps=100, active=True, applies_to="x")
    dormant = TDSClause(rate_bps=100, active=False, applies_to="x")
    assert compute_tds(1_000_000, active) == compute_tds(1_000_000, dormant)


# ---------------------------------------------------------------------------
# RateCard.version_for — effective-dated selection either side of a revision
# ---------------------------------------------------------------------------


def _two_version_card() -> RateCard:
    v1 = RateCardVersion(
        version_id="v1",
        effective_from=_dt(2026, 7, 1),
        gst_bps=1800,
        fee_rules=(_credit_rule(),),
        tds=TDSClause(rate_bps=100, active=False, applies_to="marketplace transactions"),
    )
    v2 = RateCardVersion(
        version_id="v2",
        effective_from=_dt(2026, 7, 16),
        gst_bps=1800,
        fee_rules=(_credit_rule(),),
        tds=TDSClause(rate_bps=100, active=False, applies_to="marketplace transactions"),
    )
    return RateCard(versions=(v1, v2))


def test_version_for_before_revision():
    assert _two_version_card().version_for(_dt(2026, 7, 15, 23, 59)).version_id == "v1"


def test_version_for_exactly_at_revision_effective_date_is_inclusive():
    assert _two_version_card().version_for(_dt(2026, 7, 16, 0, 0)).version_id == "v2"


def test_version_for_after_revision():
    assert _two_version_card().version_for(_dt(2026, 7, 20)).version_id == "v2"


def test_version_for_before_earliest_version_raises():
    with pytest.raises(ValueError):
        _two_version_card().version_for(_dt(2026, 6, 1))


# ---------------------------------------------------------------------------
# default_rate_card — the real, illustrative Indian PA/PG numbers
# ---------------------------------------------------------------------------


def test_default_rate_card_has_pre_and_post_revision_versions():
    card = default_rate_card(month="2026-07")
    assert len(card.versions) == 2
    v1, v2 = card.versions
    assert v1.effective_from < v2.effective_from


def test_default_rate_card_revision_moves_credit_tier1_up():
    card = default_rate_card(month="2026-07")
    v1, v2 = card.versions
    credit_v1 = v1.rule_for(PaymentMethod.CARD, CardType.CREDIT).tier_for(100_000)
    credit_v2 = v2.rule_for(PaymentMethod.CARD, CardType.CREDIT).tier_for(100_000)
    assert credit_v1.bps == 180
    assert credit_v2.bps == 195


def test_default_rate_card_revision_moves_debit_tier1_down():
    card = default_rate_card(month="2026-07")
    v1, v2 = card.versions
    debit_v1 = v1.rule_for(PaymentMethod.CARD, CardType.DEBIT).tier_for(100_000)
    debit_v2 = v2.rule_for(PaymentMethod.CARD, CardType.DEBIT).tier_for(100_000)
    assert debit_v1.bps == 90
    assert debit_v2.bps == 85


def test_default_rate_card_debit_top_tier_has_a_cap():
    card = default_rate_card(month="2026-07")
    tier = card.versions[0].rule_for(PaymentMethod.CARD, CardType.DEBIT).tier_for(5_000_000)
    assert tier.cap_paise == 15_000


def test_default_rate_card_upi_has_no_mdr_but_has_fixed_fee():
    card = default_rate_card(month="2026-07")
    rule = card.versions[0].rule_for(PaymentMethod.UPI, None)
    assert rule.tier_for(100_000).bps == 0
    assert rule.fixed_fee_paise == 200


def test_default_rate_card_tds_is_dormant():
    card = default_rate_card(month="2026-07")
    assert card.versions[0].tds.active is False
    assert card.versions[0].tds.rate_bps == 100


# ---------------------------------------------------------------------------
# render_markdown — the unstructured doc the LLM parser will eventually read
# ---------------------------------------------------------------------------


def test_render_markdown_contains_key_figures():
    card = default_rate_card(month="2026-07")
    doc = render_markdown(card, merchant_id="MERCH-0001")
    assert "1.80%" in doc
    assert "1.95%" in doc
    assert "GST" in doc
    assert "18%" in doc
    assert "194-O" in doc
    assert "MERCH-0001" in doc
