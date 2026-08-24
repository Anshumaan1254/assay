"""Golden tests for the compiled fee schedule.

Every expected figure below is hand-computed in paise and written as a
literal. Nothing in this file derives an expectation by calling the code
under test, and nothing derives one from datagen either -- except
`test_differential_against_datagen_reference`, which is explicitly a
consistency check between two independent implementations, not the
correctness proof. The literals are the proof.

The schedule under test mirrors `datagen.ratecard.default_rate_card()`:
tiered credit/debit MDR, a Rs.70 cap on the debit top slab, flat
UPI/netbanking/wallet pricing, a +2% international surcharge on cards,
18% GST on every fee line, and a mid-month addendum effective 16 Jul 2026.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from core.contract import (
    AppliesWhen,
    CompiledContract,
    ContractIncomplete,
    ContractNotSignedOff,
    ContractSignOff,
    DormantClause,
    FeeRule,
    InvalidSupersession,
    NoApplicableRule,
    OverlappingRules,
    RateCardParse,
    RoundingMode,
    TaxBase,
    TaxTreatment,
    apply_bps,
    load_signoff,
    record_signoff,
    render_schedule,
    require_signoff,
)
from core.models import IST, CardType, FeeType, Network, Payment, PaymentMethod
from core.money import Money

JUL_1 = date(2026, 7, 1)
JUL_16 = date(2026, 7, 16)

BEFORE_REVISION = datetime(2026, 7, 15, 23, 59, 59, 999999, tzinfo=IST)
AT_REVISION = datetime(2026, 7, 16, 0, 0, 0, tzinfo=IST)
GST_BPS = 1_800


# --------------------------------------------------------------------
# the reference schedule, hand-built
# --------------------------------------------------------------------


def gst() -> list[TaxTreatment]:
    return [TaxTreatment(tax_type="GST", rate_bps=GST_BPS, base=TaxBase.FEE_AMOUNT)]


def rule(
    rule_id: str,
    *,
    fee_type: FeeType = FeeType.MDR,
    effective_from: date = JUL_1,
    effective_to: date | None = None,
    supersedes: str | None = None,
    method: PaymentMethod | None = None,
    network: Network | None = None,
    card_type: CardType | None = None,
    is_international: bool | None = None,
    mcc_pattern: str | None = None,
    amount_min: int = 0,
    amount_max: int | None = None,
    rate_bps: int = 0,
    fixed: int = 0,
    cap: int | None = None,
    taxes: list[TaxTreatment] | None = None,
) -> FeeRule:
    return FeeRule(
        rule_id=rule_id,
        fee_type=fee_type,
        effective_from=effective_from,
        effective_to=effective_to,
        supersedes=supersedes,
        applies_when=AppliesWhen(
            method=method,
            network=network,
            card_type=card_type,
            is_international=is_international,
            mcc_pattern=mcc_pattern,
            amount_min_paise=amount_min,
            amount_max_paise=amount_max,
        ),
        rate_bps=rate_bps,
        fixed_fee_paise=fixed,
        cap_paise=cap,
        taxes=taxes if taxes is not None else gst(),
        source_quote=f"clause for {rule_id}",
    )


def reference_rules() -> list[FeeRule]:
    """Twelve rules. Only the two clauses the addendum actually revises are
    date-closed and superseded -- the rest simply never changed, which is
    the whole point of a flat effective-dated schedule over versioned
    snapshots."""
    card = PaymentMethod.CARD
    return [
        rule(
            "v1.card.credit.tier1",
            effective_to=JUL_16,
            method=card,
            card_type=CardType.CREDIT,
            amount_max=200_000,
            rate_bps=180,
        ),
        rule(
            "v2.card.credit.tier1",
            effective_from=JUL_16,
            supersedes="v1.card.credit.tier1",
            method=card,
            card_type=CardType.CREDIT,
            amount_max=200_000,
            rate_bps=195,
        ),
        rule(
            "card.credit.tier2",
            method=card,
            card_type=CardType.CREDIT,
            amount_min=200_000,
            amount_max=1_000_000,
            rate_bps=160,
        ),
        rule(
            "card.credit.tier3",
            method=card,
            card_type=CardType.CREDIT,
            amount_min=1_000_000,
            rate_bps=140,
        ),
        rule(
            "v1.card.debit.tier1",
            effective_to=JUL_16,
            method=card,
            card_type=CardType.DEBIT,
            amount_max=200_000,
            rate_bps=90,
        ),
        rule(
            "v2.card.debit.tier1",
            effective_from=JUL_16,
            supersedes="v1.card.debit.tier1",
            method=card,
            card_type=CardType.DEBIT,
            amount_max=200_000,
            rate_bps=85,
        ),
        rule(
            "card.debit.tier2",
            method=card,
            card_type=CardType.DEBIT,
            amount_min=200_000,
            amount_max=1_000_000,
            rate_bps=80,
        ),
        rule(
            "card.debit.tier3",
            method=card,
            card_type=CardType.DEBIT,
            amount_min=1_000_000,
            rate_bps=70,
            cap=7_000,
        ),
        rule("upi", method=PaymentMethod.UPI, rate_bps=0, fixed=200),
        rule("netbanking", method=PaymentMethod.NETBANKING, rate_bps=175, fixed=1_000),
        rule("wallet", method=PaymentMethod.WALLET, rate_bps=190, fixed=300),
        rule(
            "card.international",
            fee_type=FeeType.INTERNATIONAL,
            method=card,
            is_international=True,
            rate_bps=200,
        ),
    ]


def reference_parse(rules: list[FeeRule] | None = None) -> RateCardParse:
    return RateCardParse(
        merchant_id="MERCH-0001",
        rules=rules if rules is not None else reference_rules(),
        dormant_clauses=[
            DormantClause(
                label="TDS Section 194-O",
                stated_rate_bps=100,
                reason_not_applicable="merchant is not an e-commerce marketplace operator",
                source_quote="Not applicable - merchant is not classified as an e-commerce...",
            )
        ],
        rounding_note=None,
        parser_notes="",
    )


@pytest.fixture
def contract() -> CompiledContract:
    return CompiledContract.from_parse(reference_parse())


def payment(
    paise: int,
    method: PaymentMethod,
    *,
    card_type: CardType | None = None,
    network: Network | None = None,
    is_international: bool = False,
    mcc: str = "5411",
    at: datetime = BEFORE_REVISION,
) -> Payment:
    return Payment(
        id="pay_test",
        merchant_id="MERCH-0001",
        amount=Money(paise),
        method=method,
        network=network,
        card_type=card_type,
        is_international=is_international,
        mcc=mcc,
        captured_at=at,
        settlement_id=None,
    )


def fees_by_type(breakdown) -> dict[FeeType, int]:
    return {c.fee_type: c.amount.paise for c in breakdown.fees}


def taxes_by_fee_type(breakdown) -> dict[FeeType, int]:
    return {t.on_fee_type: t.amount.paise for t in breakdown.taxes}


# --------------------------------------------------------------------
# 1. golden vectors across every method and tier
# --------------------------------------------------------------------

CARD = PaymentMethod.CARD
CREDIT = CardType.CREDIT
DEBIT = CardType.DEBIT


@pytest.mark.parametrize(
    "gross,card_type,at,expected_mdr,expected_gst",
    [
        # credit: 1.80% before the addendum, 1.95% after; tiers 2 and 3 unchanged
        (150_000, CREDIT, BEFORE_REVISION, 2_700, 486),
        (150_000, CREDIT, AT_REVISION, 2_925, 527),  # 526.5 -> half-up 527
        (500_000, CREDIT, BEFORE_REVISION, 8_000, 1_440),
        (2_000_000, CREDIT, BEFORE_REVISION, 28_000, 5_040),
        # debit: 0.90% before, 0.85% after
        (150_000, DEBIT, BEFORE_REVISION, 1_350, 243),
        (150_000, DEBIT, AT_REVISION, 1_275, 230),  # 229.5 -> half-up 230
        (500_000, DEBIT, BEFORE_REVISION, 4_000, 720),
    ],
)
def test_golden_card_mdr(contract, gross, card_type, at, expected_mdr, expected_gst):
    breakdown = contract.fee_for(payment(gross, CARD, card_type=card_type, at=at), at)

    assert fees_by_type(breakdown) == {FeeType.MDR: expected_mdr}
    assert taxes_by_fee_type(breakdown) == {FeeType.MDR: expected_gst}
    assert breakdown.total_fee == Money(expected_mdr)
    assert breakdown.total_tax == Money(expected_gst)


def test_golden_upi_is_fixed_fee_only(contract):
    """UPI is priced at 0 bps, so no MDR component is emitted at all --
    matching datagen's `if mdr > 0` guard, not a zero-amount line."""
    breakdown = contract.fee_for(payment(50_000, PaymentMethod.UPI), BEFORE_REVISION)

    assert fees_by_type(breakdown) == {FeeType.FIXED: 200}
    assert taxes_by_fee_type(breakdown) == {FeeType.FIXED: 36}


@pytest.mark.parametrize(
    "method,gross,expected_mdr,expected_fixed,expected_mdr_gst,expected_fixed_gst",
    [
        (PaymentMethod.NETBANKING, 200_000, 3_500, 1_000, 630, 180),
        (PaymentMethod.WALLET, 100_000, 1_900, 300, 342, 54),
    ],
)
def test_golden_one_clause_emits_two_components(
    contract, method, gross, expected_mdr, expected_fixed, expected_mdr_gst, expected_fixed_gst
):
    """'1.75% + Rs.10' is one clause and two fee lines, each separately taxed."""
    breakdown = contract.fee_for(payment(gross, method), BEFORE_REVISION)

    assert fees_by_type(breakdown) == {FeeType.MDR: expected_mdr, FeeType.FIXED: expected_fixed}
    assert taxes_by_fee_type(breakdown) == {
        FeeType.MDR: expected_mdr_gst,
        FeeType.FIXED: expected_fixed_gst,
    }
    assert {c.rule_id for c in breakdown.fees} == {method.value}
    assert breakdown.total_fee == Money(expected_mdr + expected_fixed)


def test_every_component_names_the_rule_that_produced_it(contract):
    breakdown = contract.fee_for(payment(150_000, CARD, card_type=CREDIT), BEFORE_REVISION)

    assert [c.rule_id for c in breakdown.fees] == ["v1.card.credit.tier1"]
    assert [t.rule_id for t in breakdown.taxes] == ["v1.card.credit.tier1"]
    assert breakdown.contract_version == contract.version_id
    assert breakdown.rounding is RoundingMode.HALF_UP


# --------------------------------------------------------------------
# 2. the cap
# --------------------------------------------------------------------


def test_cap_not_applied_at_the_exact_boundary(contract):
    """At Rs.10,000 the 0.70% rate produces exactly the Rs.70 cap. The cap
    did not change the amount, so cap_applied is False -- D03 keys off this
    flag and must not fire on a transaction the cap never bit."""
    breakdown = contract.fee_for(payment(1_000_000, CARD, card_type=DEBIT), BEFORE_REVISION)
    (mdr,) = [c for c in breakdown.fees if c.fee_type is FeeType.MDR]

    assert mdr.amount == Money(7_000)
    assert mdr.uncapped_amount == Money(7_000)
    assert mdr.cap_paise == 7_000
    assert mdr.cap_applied is False


def test_cap_applied_above_the_boundary(contract):
    breakdown = contract.fee_for(payment(2_000_000, CARD, card_type=DEBIT), BEFORE_REVISION)
    (mdr,) = [c for c in breakdown.fees if c.fee_type is FeeType.MDR]

    assert mdr.uncapped_amount == Money(14_000)
    assert mdr.amount == Money(7_000)
    assert mdr.cap_applied is True
    assert taxes_by_fee_type(breakdown) == {FeeType.MDR: 1_260}  # GST on the capped figure


def test_cap_does_not_clamp_the_fixed_fee():
    """Confirmed decision: cap_paise caps the ad-valorem component only."""
    rules = [
        rule("capped", method=PaymentMethod.WALLET, rate_bps=1_000, fixed=5_000, cap=100),
    ]
    contract = CompiledContract.from_parse(reference_parse(rules))
    breakdown = contract.fee_for(payment(100_000, PaymentMethod.WALLET), BEFORE_REVISION)

    assert fees_by_type(breakdown) == {FeeType.MDR: 100, FeeType.FIXED: 5_000}


# --------------------------------------------------------------------
# 3. the international surcharge
# --------------------------------------------------------------------


def test_international_surcharge_stacks_on_mdr(contract):
    at = BEFORE_REVISION
    p = payment(150_000, CARD, card_type=CREDIT, network=Network.VISA, is_international=True, at=at)
    breakdown = contract.fee_for(p, at)

    assert fees_by_type(breakdown) == {FeeType.MDR: 2_700, FeeType.INTERNATIONAL: 3_000}
    assert taxes_by_fee_type(breakdown) == {FeeType.MDR: 486, FeeType.INTERNATIONAL: 540}
    assert breakdown.total_fee == Money(5_700)
    assert breakdown.total_tax == Money(1_026)


def test_domestic_card_has_no_surcharge_component(contract):
    breakdown = contract.fee_for(payment(150_000, CARD, card_type=CREDIT), BEFORE_REVISION)

    assert FeeType.INTERNATIONAL not in fees_by_type(breakdown)


def test_international_surcharge_applies_to_debit_too(contract):
    p = payment(150_000, CARD, card_type=DEBIT, is_international=True)
    breakdown = contract.fee_for(p, BEFORE_REVISION)

    assert fees_by_type(breakdown) == {FeeType.MDR: 1_350, FeeType.INTERNATIONAL: 3_000}


def test_international_upi_gets_no_card_surcharge(contract):
    """The surcharge clause is scoped to cards; a non-card method must not
    pick it up just because is_international is true."""
    p = payment(50_000, PaymentMethod.UPI, is_international=True)
    breakdown = contract.fee_for(p, BEFORE_REVISION)

    assert fees_by_type(breakdown) == {FeeType.FIXED: 200}


# --------------------------------------------------------------------
# 4. effective dating, at the boundary
# --------------------------------------------------------------------


def test_last_instant_before_midnight_uses_the_old_rate(contract):
    breakdown = contract.fee_for(payment(150_000, CARD, card_type=CREDIT), BEFORE_REVISION)

    assert [c.rule_id for c in breakdown.fees] == ["v1.card.credit.tier1"]
    assert breakdown.total_fee == Money(2_700)


def test_exactly_midnight_on_the_revision_date_uses_the_new_rate(contract):
    """[effective_from, effective_to) -- half-open. At exactly
    2026-07-16T00:00:00+05:30 the addendum is in force."""
    breakdown = contract.fee_for(payment(150_000, CARD, card_type=CREDIT), AT_REVISION)

    assert [c.rule_id for c in breakdown.fees] == ["v2.card.credit.tier1"]
    assert breakdown.total_fee == Money(2_925)


def test_one_microsecond_apart_straddles_the_boundary(contract):
    p = payment(150_000, CARD, card_type=CREDIT)
    before = contract.fee_for(p, AT_REVISION - timedelta(microseconds=1))
    after = contract.fee_for(p, AT_REVISION)

    assert before.total_fee == Money(2_700)
    assert after.total_fee == Money(2_925)


@pytest.mark.parametrize(
    "utc_instant,expected_fee",
    [
        # 18:00Z on the 15th is 23:30 IST on the 15th -- still the old rate
        (datetime(2026, 7, 15, 18, 0, tzinfo=UTC), 2_700),
        # 20:00Z on the 15th is 01:30 IST on the 16th -- the new rate
        (datetime(2026, 7, 15, 20, 0, tzinfo=UTC), 2_925),
    ],
)
def test_boundary_is_resolved_in_ist_not_utc(contract, utc_instant, expected_fee):
    breakdown = contract.fee_for(payment(150_000, CARD, card_type=CREDIT), utc_instant)

    assert breakdown.total_fee == Money(expected_fee)


def test_naive_datetime_is_refused(contract):
    with pytest.raises(ValueError, match="timezone-aware"):
        contract.fee_for(payment(150_000, CARD, card_type=CREDIT), datetime(2026, 7, 16))  # noqa: DTZ001


def test_before_the_schedule_starts_has_no_applicable_rule(contract):
    with pytest.raises(NoApplicableRule):
        contract.fee_for(
            payment(150_000, CARD, card_type=CREDIT), datetime(2026, 6, 30, 12, 0, tzinfo=IST)
        )


# --------------------------------------------------------------------
# 5. overlap is a hard error
# --------------------------------------------------------------------


def test_two_rules_matching_the_same_transaction_raise():
    rules = reference_rules() + [
        rule(
            "promo.credit",
            method=CARD,
            card_type=CREDIT,
            amount_max=50_000,
            rate_bps=100,
        )
    ]
    with pytest.raises(OverlappingRules) as excinfo:
        CompiledContract.from_parse(reference_parse(rules))

    message = str(excinfo.value)
    assert "promo.credit" in message
    assert "v1.card.credit.tier1" in message
    assert "clause for promo.credit" in message  # names the clause, not just the id


def test_overlap_is_scoped_to_the_component_type_a_rule_emits(contract):
    """The +2% surcharge matches every international card transaction that
    the MDR clause also matches. That must stay legal: they emit different
    component types and stack."""
    assert contract.version_id  # constructed without raising
    p = payment(150_000, CARD, card_type=CREDIT, is_international=True)
    assert len(contract.fee_for(p, BEFORE_REVISION).fees) == 2


def test_two_surcharges_of_the_same_type_do_overlap():
    rules = reference_rules() + [
        rule(
            "card.international.duplicate",
            fee_type=FeeType.INTERNATIONAL,
            method=CARD,
            is_international=True,
            rate_bps=250,
        )
    ]
    with pytest.raises(OverlappingRules):
        CompiledContract.from_parse(reference_parse(rules))


def test_two_rules_both_emitting_fixed_fees_overlap():
    """Different declared fee_type, same emitted FIXED component."""
    rules = [
        rule("base", method=PaymentMethod.UPI, fixed=200),
        rule("extra", fee_type=FeeType.DISPUTE_FEE, method=PaymentMethod.UPI, rate_bps=50, fixed=100),
    ]
    with pytest.raises(OverlappingRules):
        CompiledContract.from_parse(reference_parse(rules))


def test_adjacent_amount_bands_do_not_overlap():
    """[0, 200000) and [200000, 1000000) share an endpoint but no value."""
    contract = CompiledContract.from_parse(
        reference_parse(
            [
                rule("low", method=PaymentMethod.UPI, amount_max=200_000, rate_bps=100),
                rule("high", method=PaymentMethod.UPI, amount_min=200_000, rate_bps=50),
            ]
        )
    )
    assert contract.fee_for(payment(199_999, PaymentMethod.UPI), BEFORE_REVISION).fees[0].rate_bps == 100
    assert contract.fee_for(payment(200_000, PaymentMethod.UPI), BEFORE_REVISION).fees[0].rate_bps == 50


def test_mcc_prefix_globs_that_cannot_both_match_do_not_overlap():
    contract = CompiledContract.from_parse(
        reference_parse(
            [
                rule("groceries", method=PaymentMethod.UPI, mcc_pattern="54*", rate_bps=100),
                rule("restaurants", method=PaymentMethod.UPI, mcc_pattern="58*", rate_bps=50),
            ]
        )
    )
    assert contract.fee_for(payment(1_000, PaymentMethod.UPI, mcc="5411"), BEFORE_REVISION).fees[0].rate_bps == 100
    assert contract.fee_for(payment(1_000, PaymentMethod.UPI, mcc="5812"), BEFORE_REVISION).fees[0].rate_bps == 50


def test_mcc_glob_containing_a_literal_overlaps_it():
    rules = [
        rule("broad", method=PaymentMethod.UPI, mcc_pattern="5*", rate_bps=100),
        rule("narrow", method=PaymentMethod.UPI, mcc_pattern="5411", rate_bps=50),
    ]
    with pytest.raises(OverlappingRules):
        CompiledContract.from_parse(reference_parse(rules))


# --------------------------------------------------------------------
# 6. completeness
# --------------------------------------------------------------------


def test_amount_gap_is_rejected():
    rules = [
        rule("low", method=PaymentMethod.UPI, amount_max=100_000, rate_bps=100),
        rule("high", method=PaymentMethod.UPI, amount_min=200_000, rate_bps=50),
    ]
    with pytest.raises(ContractIncomplete, match="100000"):
        CompiledContract.from_parse(reference_parse(rules))


def test_unbounded_top_band_is_required():
    rules = [rule("only", method=PaymentMethod.UPI, amount_max=100_000, rate_bps=100)]
    with pytest.raises(ContractIncomplete):
        CompiledContract.from_parse(reference_parse(rules))


def test_date_gap_is_rejected():
    rules = [
        rule("early", effective_to=date(2026, 7, 10), method=PaymentMethod.UPI, rate_bps=100),
        rule("late", effective_from=JUL_16, method=PaymentMethod.UPI, rate_bps=50),
    ]
    with pytest.raises(ContractIncomplete):
        CompiledContract.from_parse(reference_parse(rules))


def test_uncovered_method_is_reported_not_invented(contract):
    """The rate card prices no EMI clause. The right answer for an EMI
    transaction is an exception, never a guessed fee."""
    assert PaymentMethod.EMI in contract.uncovered_methods

    with pytest.raises(NoApplicableRule, match="emi"):
        contract.fee_for(payment(150_000, PaymentMethod.EMI), BEFORE_REVISION)


def test_surcharge_rules_are_exempt_from_coverage(contract):
    """`card.international` covers only international cards and would fail
    any gapless-coverage test. Only MDR clauses carry that obligation."""
    assert contract.version_id


# --------------------------------------------------------------------
# 7. rounding
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected",
    [
        (RoundingMode.HALF_UP, 11),
        (RoundingMode.HALF_EVEN, 10),
        (RoundingMode.UP, 11),
        (RoundingMode.DOWN, 10),
    ],
)
def test_rounding_modes_diverge_on_an_exact_tie(mode, expected):
    """Rs.6.00 at 1.75% is exactly 10.5 paise. Half-even rounds down to the
    even 10; half-up goes to 11. This is precisely the divergence datagen's
    D07 injector plants, so the engine has to be able to tell them apart."""
    contract = CompiledContract.from_parse(reference_parse(), rounding=mode)
    breakdown = contract.fee_for(payment(600, PaymentMethod.NETBANKING), BEFORE_REVISION)

    assert fees_by_type(breakdown)[FeeType.MDR] == expected
    assert breakdown.rounding is mode


@pytest.mark.parametrize(
    "mode,expected",
    [
        (RoundingMode.HALF_UP, 2),
        (RoundingMode.HALF_EVEN, 2),
        (RoundingMode.UP, 2),
        (RoundingMode.DOWN, 1),
    ],
)
def test_rounding_modes_on_a_non_tie(mode, expected):
    """1.00 paise at 1.75% is 1.75 -- above the tie, so only DOWN differs."""
    assert apply_bps(100, 175, mode) == expected


def test_apply_bps_is_exact_when_there_is_no_remainder():
    for mode in RoundingMode:
        assert apply_bps(150_000, 180, mode) == 2_700


def test_apply_bps_rounds_negative_amounts_away_from_zero_on_half_up():
    assert apply_bps(-600, 175, RoundingMode.HALF_UP) == -11
    assert apply_bps(-600, 175, RoundingMode.DOWN) == -10


def test_rounding_mode_is_part_of_the_contract_version():
    """Changing the rounding mode changes every computed amount, so it must
    change the version id -- otherwise two runs could disagree while
    reporting the same contract_version in AuditRun."""
    half_up = CompiledContract.from_parse(reference_parse(), rounding=RoundingMode.HALF_UP)
    half_even = CompiledContract.from_parse(reference_parse(), rounding=RoundingMode.HALF_EVEN)

    assert half_up.version_id != half_even.version_id


# --------------------------------------------------------------------
# 8. schema validation -- out-of-enum values reject the whole parse
# --------------------------------------------------------------------


def test_unknown_network_is_rejected():
    with pytest.raises(ValueError):
        AppliesWhen(network="diners club")


def test_unknown_method_is_rejected():
    with pytest.raises(ValueError):
        AppliesWhen(method="cryptocurrency")


def test_unknown_fee_type_is_rejected():
    with pytest.raises(ValueError):
        rule("bad", fee_type="convenience_fee")


def test_extra_fields_are_forbidden():
    with pytest.raises(ValueError):
        AppliesWhen(method=PaymentMethod.UPI, merchant_tier="gold")


# --------------------------------------------------------------------
# 9. field-shape validation
# --------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["54[0-9]+", ".*", "54?", "abc", "5411*1", ""])
def test_regex_shaped_mcc_patterns_are_rejected(bad):
    with pytest.raises(ValueError):
        AppliesWhen(mcc_pattern=bad)


@pytest.mark.parametrize("good", ["5411", "54*", "5*", "0*"])
def test_prefix_glob_mcc_patterns_are_accepted(good):
    assert AppliesWhen(mcc_pattern=good).mcc_pattern == good


@pytest.mark.parametrize("bad", ["Credit Tier 1", "-leading", "", "a" * 65])
def test_malformed_rule_ids_are_rejected(bad):
    with pytest.raises(ValueError):
        rule(bad)


def test_duplicate_rule_ids_are_rejected():
    with pytest.raises(ValueError, match="duplicate"):
        reference_parse([rule("dup", method=PaymentMethod.UPI), rule("dup", method=PaymentMethod.WALLET)])


def test_inverted_amount_band_is_rejected():
    with pytest.raises(ValueError):
        AppliesWhen(amount_min_paise=200_000, amount_max_paise=100_000)


def test_negative_rate_is_rejected():
    with pytest.raises(ValueError):
        rule("neg", method=PaymentMethod.UPI, rate_bps=-1)


def test_inverted_effective_range_is_rejected():
    with pytest.raises(ValueError):
        rule("backwards", effective_from=JUL_16, effective_to=JUL_1, method=PaymentMethod.UPI)


def test_cap_without_a_rate_is_rejected():
    """A cap on a zero ad-valorem rate can never bind -- much more likely a
    misparse than a real clause."""
    with pytest.raises(ValueError, match="cap"):
        rule("dead_cap", method=PaymentMethod.UPI, rate_bps=0, fixed=200, cap=5_000)


# --------------------------------------------------------------------
# 10. supersession is validated, never inferred
# --------------------------------------------------------------------


def test_supersedes_must_name_an_existing_rule():
    with pytest.raises(ValueError, match="ghost"):
        reference_parse([rule("real", method=PaymentMethod.UPI, supersedes="ghost")])


def test_supersession_must_be_date_contiguous():
    rules = [
        rule("old", effective_to=date(2026, 7, 10), method=PaymentMethod.UPI, rate_bps=100),
        rule("new", effective_from=JUL_16, supersedes="old", method=PaymentMethod.UPI, rate_bps=50),
    ]
    with pytest.raises((InvalidSupersession, ContractIncomplete)):
        CompiledContract.from_parse(reference_parse(rules))


def test_supersession_must_match_on_applies_when():
    rules = [
        rule("old", effective_to=JUL_16, method=PaymentMethod.UPI, rate_bps=100),
        rule("new", effective_from=JUL_16, supersedes="old", method=PaymentMethod.WALLET, rate_bps=50),
    ]
    with pytest.raises((InvalidSupersession, ContractIncomplete)):
        CompiledContract.from_parse(reference_parse(rules))


def test_a_dangling_effective_to_surfaces_as_an_overlap():
    """If the model forgets to close the superseded clause, both versions
    match from the revision date onward. That is reported, not silently
    repaired -- closing it for the model would be the engine inventing a
    business rule."""
    rules = [r for r in reference_rules() if not r.rule_id.endswith("card.credit.tier1")]
    rules += [
        # effective_to left open, and so `supersedes` left unset too
        rule("v1.card.credit.tier1", method=CARD, card_type=CREDIT, amount_max=200_000, rate_bps=180),
        rule(
            "v2.card.credit.tier1",
            effective_from=JUL_16,
            method=CARD,
            card_type=CREDIT,
            amount_max=200_000,
            rate_bps=195,
        ),
    ]

    with pytest.raises(OverlappingRules):
        CompiledContract.from_parse(reference_parse(rules))


# --------------------------------------------------------------------
# 11. determinism
# --------------------------------------------------------------------


def test_version_id_is_stable_across_identical_parses():
    assert CompiledContract.from_parse(reference_parse()).version_id == (
        CompiledContract.from_parse(reference_parse()).version_id
    )


def test_version_id_ignores_rule_ordering():
    shuffled = list(reversed(reference_rules()))
    assert CompiledContract.from_parse(reference_parse(shuffled)).version_id == (
        CompiledContract.from_parse(reference_parse()).version_id
    )


def test_version_id_changes_when_a_rate_changes():
    rules = reference_rules()
    rules[0] = rule(
        "v1.card.credit.tier1",
        effective_to=JUL_16,
        method=CARD,
        card_type=CREDIT,
        amount_max=200_000,
        rate_bps=181,
    )
    assert CompiledContract.from_parse(reference_parse(rules)).version_id != (
        CompiledContract.from_parse(reference_parse()).version_id
    )


def test_version_id_is_a_prefixed_hash(contract):
    assert contract.version_id == f"rc-{contract.schedule_sha256[:12]}"


def test_provenance_does_not_change_the_version_id():
    """The version identifies the arithmetic, not the input bytes."""
    a = CompiledContract.from_parse(reference_parse(), source_sha256="a" * 64)
    b = CompiledContract.from_parse(reference_parse(), source_sha256="b" * 64)

    assert a.version_id == b.version_id
    assert a.source_sha256 != b.source_sha256


def test_round_trips_through_disk(tmp_path, contract):
    path = tmp_path / "contract.json"
    contract.save(path)
    reloaded = CompiledContract.load(path)

    assert reloaded.version_id == contract.version_id
    assert reloaded.rounding is contract.rounding
    p = payment(150_000, CARD, card_type=CREDIT)
    assert reloaded.fee_for(p, BEFORE_REVISION).total_fee == contract.fee_for(p, BEFORE_REVISION).total_fee


def test_canonical_json_is_byte_stable(contract):
    assert contract.canonical_json() == CompiledContract.from_parse(reference_parse()).canonical_json()


# --------------------------------------------------------------------
# 12. the round-trip render and sign-off
# --------------------------------------------------------------------


def test_render_mentions_every_rule_and_the_dormant_clause(contract):
    rendered = render_schedule(contract)

    for r in contract.rules:
        assert r.rule_id in rendered
    assert "TDS Section 194-O" in rendered
    assert "not an e-commerce marketplace operator" in rendered
    assert contract.version_id in rendered
    assert "half_up" in rendered


def test_render_is_deterministic(contract):
    assert render_schedule(contract) == render_schedule(CompiledContract.from_parse(reference_parse()))


def test_unsigned_contract_is_refused(tmp_path, contract):
    with pytest.raises(ContractNotSignedOff):
        require_signoff(contract, tmp_path)


def test_signed_contract_is_accepted(tmp_path, contract):
    record_signoff(contract, tmp_path, signed_off_by="anshumaan", signed_off_at=AT_REVISION)
    signoff = require_signoff(contract, tmp_path)

    assert isinstance(signoff, ContractSignOff)
    assert signoff.signed_off_by == "anshumaan"
    assert signoff.schedule_sha256 == contract.schedule_sha256


def test_signoff_does_not_transfer_to_a_changed_schedule(tmp_path, contract):
    record_signoff(contract, tmp_path, signed_off_by="anshumaan", signed_off_at=AT_REVISION)

    rules = reference_rules()
    rules[0] = rule(
        "v1.card.credit.tier1",
        effective_to=JUL_16,
        method=CARD,
        card_type=CREDIT,
        amount_max=200_000,
        rate_bps=250,
    )
    tampered = CompiledContract.from_parse(reference_parse(rules))

    with pytest.raises(ContractNotSignedOff):
        require_signoff(tampered, tmp_path)


def test_replay_finds_the_existing_signoff_without_re_prompting(tmp_path, contract):
    record_signoff(contract, tmp_path, signed_off_by="anshumaan", signed_off_at=AT_REVISION)
    rebuilt = CompiledContract.from_parse(reference_parse())

    assert load_signoff(rebuilt, tmp_path) is not None


# --------------------------------------------------------------------
# 13. consistency check against datagen's independent calculator
# --------------------------------------------------------------------


def test_differential_against_datagen_reference():
    """NOT the correctness proof -- the literals above are. This checks that
    two independently written implementations (Decimal-and-quantize in
    datagen, pure-integer here) agree across the generated month's shape."""
    from datagen.ratecard import default_rate_card, gst_amount, mdr_amount, mdr_raw_amount

    contract = CompiledContract.from_parse(reference_parse())
    card = default_rate_card("2026-07")

    cases = [
        (gross, method, card_type, is_intl, at)
        for gross in (4_900, 150_000, 199_999, 200_000, 999_999, 1_000_000, 2_000_000, 20_000_000)
        for method, card_type in (
            (CARD, CREDIT),
            (CARD, DEBIT),
            (PaymentMethod.UPI, None),
            (PaymentMethod.NETBANKING, None),
            (PaymentMethod.WALLET, None),
        )
        for is_intl in (False, True)
        for at in (BEFORE_REVISION, AT_REVISION)
    ]

    for gross, method, card_type, is_intl, at in cases:
        version = card.version_for(at)
        ref_rule = version.rule_for(method, card_type)
        expected_fees: dict[FeeType, int] = {}

        mdr = mdr_amount(gross, ref_rule.tier_for(gross))
        if mdr > 0:
            expected_fees[FeeType.MDR] = mdr
        if ref_rule.fixed_fee_paise > 0:
            expected_fees[FeeType.FIXED] = ref_rule.fixed_fee_paise
        if is_intl and ref_rule.international_surcharge_bps > 0:
            surcharge = mdr_raw_amount(gross, ref_rule.international_surcharge_bps)
            if surcharge > 0:
                expected_fees[FeeType.INTERNATIONAL] = surcharge

        p = payment(gross, method, card_type=card_type, is_international=is_intl, at=at)
        breakdown = contract.fee_for(p, at)

        assert fees_by_type(breakdown) == expected_fees, (gross, method, card_type, is_intl, at)
        assert taxes_by_fee_type(breakdown) == {
            fee_type: gst_amount(amount, version.gst_bps) for fee_type, amount in expected_fees.items()
        }, (gross, method, card_type, is_intl, at)


# --------------------------------------------------------------------
# 14. review findings — tax base, cap evidence, unpriced card types
# --------------------------------------------------------------------


def gross_taxed_rule(rate_bps: int = 175, fixed: int = 1_000, cap: int | None = None) -> FeeRule:
    """One clause charging a percentage AND a flat fee, with a tax levied on
    the transaction gross rather than on the fee -- the shape a TDS clause
    takes."""
    return rule(
        "netbanking",
        method=PaymentMethod.NETBANKING,
        rate_bps=rate_bps,
        fixed=fixed,
        cap=cap,
        taxes=[TaxTreatment(tax_type="TDS", rate_bps=GST_BPS, base=TaxBase.TRANSACTION_GROSS)],
    )


def test_a_gross_based_tax_is_charged_once_however_many_fee_lines_the_clause_emits():
    """A tax on the transaction gross is a property of the transaction, not
    of a fee line. Computing it per component multiplies the same base:
    "1.75% + Rs.10" emits two fee lines, and a naive per-component loop bills
    the gross tax twice."""
    contract = CompiledContract.from_parse(reference_parse([gross_taxed_rule()]))
    breakdown = contract.fee_for(payment(100_000, PaymentMethod.NETBANKING), BEFORE_REVISION)

    assert fees_by_type(breakdown) == {FeeType.MDR: 1_750, FeeType.FIXED: 1_000}
    assert [t.amount.paise for t in breakdown.taxes] == [18_000]
    assert breakdown.total_tax == Money(18_000)


def test_a_gross_based_tax_is_not_attributed_to_any_one_fee_line():
    contract = CompiledContract.from_parse(reference_parse([gross_taxed_rule()]))
    (tax,) = contract.fee_for(payment(100_000, PaymentMethod.NETBANKING), BEFORE_REVISION).taxes

    assert tax.on_fee_type is None
    assert tax.base is TaxBase.TRANSACTION_GROSS
    assert tax.base_amount == Money(100_000)
    assert tax.rule_id == "netbanking"


def test_a_gross_based_tax_applies_even_when_the_clause_charges_no_fee():
    """A zero-rated clause still matched, so its tax treatment still binds."""
    zero_rated = rule(
        "netbanking",
        method=PaymentMethod.NETBANKING,
        rate_bps=0,
        fixed=0,
        taxes=[TaxTreatment(tax_type="TDS", rate_bps=100, base=TaxBase.TRANSACTION_GROSS)],
    )
    contract = CompiledContract.from_parse(reference_parse([zero_rated]))
    breakdown = contract.fee_for(payment(100_000, PaymentMethod.NETBANKING), BEFORE_REVISION)

    assert breakdown.fees == []
    assert breakdown.total_tax == Money(1_000)


def test_a_fee_based_tax_stays_per_fee_line(contract):
    """The control for the two tests above: GST on fee amounts is genuinely
    per-line, because each line is a distinct taxable amount."""
    breakdown = contract.fee_for(payment(200_000, PaymentMethod.NETBANKING), BEFORE_REVISION)

    assert taxes_by_fee_type(breakdown) == {FeeType.MDR: 630, FeeType.FIXED: 180}
    assert all(t.on_fee_type is not None for t in breakdown.taxes)


def test_a_cap_of_zero_still_reports_that_the_cap_bit():
    """A cap that waives the fee entirely is the case where the evidence
    matters most: without a component, a verifier has nothing to compare a
    wrongly-charged settlement line against."""
    waived = rule("wallet", method=PaymentMethod.WALLET, rate_bps=175, fixed=0, cap=0)
    contract = CompiledContract.from_parse(reference_parse([waived]))
    breakdown = contract.fee_for(payment(100_000, PaymentMethod.WALLET), BEFORE_REVISION)

    (mdr,) = breakdown.fees
    assert mdr.amount == Money(0)
    assert mdr.uncapped_amount == Money(1_750)
    assert mdr.cap_applied is True
    assert breakdown.total_fee == Money(0)


def test_a_fee_rounding_to_zero_emits_no_component(contract):
    """Distinct from the cap case: nothing was capped, the fee genuinely
    rounds to nothing. Mirrors datagen's `if mdr > 0` guard."""
    breakdown = contract.fee_for(payment(1, PaymentMethod.WALLET), BEFORE_REVISION)

    assert FeeType.MDR not in fees_by_type(breakdown)


def test_zero_gross_payment_emits_no_fee(contract):
    breakdown = contract.fee_for(payment(0, CARD, card_type=CREDIT), BEFORE_REVISION)

    assert breakdown.fees == []
    assert breakdown.taxes == []
    assert breakdown.matched_rule_ids == ["v1.card.credit.tier1"]
    assert breakdown.total_fee == Money(0)


def test_prepaid_cards_are_reported_as_unpriced(contract):
    """The card slabs price credit and debit only. `uncovered_methods` looks
    at method alone, so it says CARD is priced -- true, but a prepaid card
    still has no clause. A human reading the sign-off render must be told."""
    assert PaymentMethod.CARD not in contract.uncovered_methods
    assert CardType.PREPAID in contract.uncovered_card_types

    with pytest.raises(NoApplicableRule, match="prepaid"):
        contract.fee_for(payment(150_000, CARD, card_type=CardType.PREPAID), BEFORE_REVISION)


def test_priced_card_types_are_not_reported_as_unpriced(contract):
    assert CardType.CREDIT not in contract.uncovered_card_types
    assert CardType.DEBIT not in contract.uncovered_card_types


def test_render_names_the_unpriced_card_type(contract):
    rendered = render_schedule(contract)

    assert "prepaid" in rendered
    assert "emi" in rendered


def test_a_card_with_no_card_type_has_no_clause(contract):
    with pytest.raises(NoApplicableRule):
        contract.fee_for(payment(150_000, CARD, card_type=None), BEFORE_REVISION)


# --------------------------------------------------------------------
# 15. review findings — rounding parity, tier boundaries, date overlap
# --------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected",
    [
        (RoundingMode.HALF_EVEN, 32),
        (RoundingMode.HALF_UP, 32),
        (RoundingMode.UP, 32),
        (RoundingMode.DOWN, 31),
    ],
)
def test_half_even_rounds_up_at_an_odd_tie(mode, expected):
    """Rs.18.00 at 1.75% is exactly 31.5 paise, and 31 is odd, so half-even
    must round UP to the even 32. The existing 10.5 case has an even whole
    part, where half-even and truncation happen to agree -- on its own it
    cannot tell the parity rule from 'always round down at a tie'."""
    assert apply_bps(1_800, 175, mode) == expected


@pytest.mark.parametrize(
    "gross,card_type,expected_mdr,expected_gst",
    [
        # tier1 -> tier2 boundary at Rs.2,000
        (199_999, CREDIT, 3_600, 648),
        (200_000, CREDIT, 3_200, 576),
        (199_999, DEBIT, 1_800, 324),
        (200_000, DEBIT, 1_600, 288),
        # tier2 -> tier3 boundary at Rs.10,000
        (999_999, CREDIT, 16_000, 2_880),
        (1_000_000, CREDIT, 14_000, 2_520),
        (999_999, DEBIT, 8_000, 1_440),
    ],
)
def test_golden_at_the_real_tier_boundaries(contract, gross, card_type, expected_mdr, expected_gst):
    """The slab edges of the actual rate card, hand-computed. Previously
    these amounts appeared only in the differential test, which the module
    docstring explicitly disclaims as a correctness proof."""
    breakdown = contract.fee_for(payment(gross, CARD, card_type=card_type), BEFORE_REVISION)

    assert fees_by_type(breakdown) == {FeeType.MDR: expected_mdr}
    assert taxes_by_fee_type(breakdown) == {FeeType.MDR: expected_gst}


@pytest.mark.parametrize(
    "gross,card_type,expected_mdr",
    [
        (500_000, CREDIT, 8_000),
        (2_000_000, CREDIT, 28_000),
        (500_000, DEBIT, 4_000),
        (2_000_000, DEBIT, 7_000),
    ],
)
def test_the_addendum_leaves_tiers_two_and_three_untouched(contract, gross, card_type, expected_mdr):
    """The addendum revises slab 1 only. Tiers 2 and 3 share a completeness
    group with the revised slab, so a change that perturbed their date range
    while staying gapless and non-overlapping would pass every other test."""
    breakdown = contract.fee_for(payment(gross, CARD, card_type=card_type, at=AT_REVISION), AT_REVISION)

    assert fees_by_type(breakdown) == {FeeType.MDR: expected_mdr}


def test_partially_offset_date_ranges_overlap():
    """Both prior overlap tests drove the conflict through the amount or MCC
    axis, leaving `_dates_intersect`'s genuinely-offset branch unexercised."""
    rules = [
        rule("early", effective_to=date(2026, 7, 20), method=PaymentMethod.UPI, rate_bps=100),
        rule("late", effective_from=date(2026, 7, 15), method=PaymentMethod.UPI, rate_bps=50),
    ]
    with pytest.raises(OverlappingRules):
        CompiledContract.from_parse(reference_parse(rules))
