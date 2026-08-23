"""Builds the "true" world for one generated settlement month: payments,
refunds, chargebacks, reserve/manual adjustments, T+2 settlement batches,
and bank credits -- all correctly computed and correctly assigned, before
datagen/inject.py plants any deliberate discrepancy.

Every record's settlement_id is the T+2 batch for the day of its own
primary timestamp (captured_at for a Payment, created_at for a
Refund/Adjustment, raised_at for a Chargeback) -- this one rule is what
naturally produces the cross-cycle-refund case the spec calls out, with no
special-casing.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from random import Random

from core.models import (
    IST,
    Adjustment,
    AdjustmentKind,
    BankCredit,
    BatchStatus,
    CardType,
    Chargeback,
    ChargebackStage,
    EntityType,
    FeeLine,
    FeeType,
    Payment,
    PaymentMethod,
    Refund,
    SettlementBatch,
    TaxLine,
)
from core.money import Money
from datagen.config import GenerationConfig
from datagen.ratecard import RateCard, gst_amount, mdr_amount, mdr_raw_amount
from datagen.sampling import sample_amount_paise, sample_card_type, sample_method, sample_network
from datagen.timeline import (
    credit_value_date,
    day_weight,
    distribute_counts,
    ist_moment,
    month_bounds,
    settlement_batch_id,
)

_ADD_BACK_KINDS = {AdjustmentKind.RESERVE_RELEASE, AdjustmentKind.MANUAL_CREDIT, AdjustmentKind.FEE_WAIVER}
_SUBTRACT_KINDS = {AdjustmentKind.RESERVE_HOLD, AdjustmentKind.MANUAL_DEBIT}

_BANK_PREFIXES = ("NEFT-HDFC0001234-", "IMPS/AXIS/", "RTGS INDBANK ", "UPI/ICICI/")


class _IdSequence:
    def __init__(self, prefix: str, width: int = 6):
        self._prefix = prefix
        self._width = width
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"{self._prefix}-{self._n:0{self._width}d}"


class World:
    """A plain, mutable container of frozen records. Lists are mutable so
    datagen/inject.py can build a "reported" World by shallow-copying this
    one and splicing in modified/added/removed records; individual records
    stay frozen Pydantic models throughout."""

    def __init__(
        self,
        payments: list[Payment],
        refunds: list[Refund],
        chargebacks: list[Chargeback],
        adjustments: list[Adjustment],
        fee_lines: list[FeeLine],
        tax_lines: list[TaxLine],
        batches: list[SettlementBatch],
        bank_credits: list[BankCredit],
    ) -> None:
        self.payments = payments
        self.refunds = refunds
        self.chargebacks = chargebacks
        self.adjustments = adjustments
        self.fee_lines = fee_lines
        self.tax_lines = tax_lines
        self.batches = batches
        self.bank_credits = bank_credits

    def copy(self) -> World:
        return World(
            payments=list(self.payments),
            refunds=list(self.refunds),
            chargebacks=list(self.chargebacks),
            adjustments=list(self.adjustments),
            fee_lines=list(self.fee_lines),
            tax_lines=list(self.tax_lines),
            batches=list(self.batches),
            bank_credits=list(self.bank_credits),
        )


def _compute_fee_and_tax_lines(
    payment: Payment, rate_card: RateCard, fee_ids: _IdSequence, tax_ids: _IdSequence
) -> tuple[list[FeeLine], list[TaxLine]]:
    version = rate_card.version_for(payment.captured_at)
    rule = version.rule_for(payment.method, payment.card_type)
    gross = payment.amount.paise

    fee_lines: list[FeeLine] = []
    if rule.tiers:
        tier = rule.tier_for(gross)
        mdr = mdr_amount(gross, tier)
        if mdr > 0:
            fee_lines.append(
                FeeLine(
                    id=fee_ids.next(),
                    applies_to_id=payment.id,
                    applies_to_type=EntityType.PAYMENT,
                    fee_type=FeeType.MDR,
                    computed_amount=Money(mdr),
                    rule_id=f"{version.version_id}:{payment.method.value}:{payment.card_type.value if payment.card_type else 'flat'}:mdr",
                )
            )
    if rule.fixed_fee_paise > 0:
        fee_lines.append(
            FeeLine(
                id=fee_ids.next(),
                applies_to_id=payment.id,
                applies_to_type=EntityType.PAYMENT,
                fee_type=FeeType.FIXED,
                computed_amount=Money(rule.fixed_fee_paise),
                rule_id=f"{version.version_id}:{payment.method.value}:fixed",
            )
        )
    if payment.is_international and rule.international_surcharge_bps > 0:
        surcharge = mdr_raw_amount(gross, rule.international_surcharge_bps)
        if surcharge > 0:
            fee_lines.append(
                FeeLine(
                    id=fee_ids.next(),
                    applies_to_id=payment.id,
                    applies_to_type=EntityType.PAYMENT,
                    fee_type=FeeType.INTERNATIONAL,
                    computed_amount=Money(surcharge),
                    rule_id=f"{version.version_id}:international",
                )
            )

    tax_lines: list[TaxLine] = []
    for fee in fee_lines:
        tax = gst_amount(fee.computed_amount.paise, version.gst_bps)
        tax_lines.append(
            TaxLine(
                id=tax_ids.next(),
                applies_to_fee_id=fee.id,
                tax_type="GST",
                rate_bps=version.gst_bps,
                base_amount=fee.computed_amount,
                amount=Money(tax),
            )
        )
    return fee_lines, tax_lines


def _generate_payments(
    config: GenerationConfig, rate_card: RateCard, rng: Random
) -> tuple[list[Payment], list[FeeLine], list[TaxLine]]:
    first_day, last_day = month_bounds(config.month)
    days = [first_day + timedelta(days=i) for i in range((last_day - first_day).days + 1)]
    weights = [
        day_weight(d, config.festival_week, config.festival_multiplier, config.weekend_multiplier) for d in days
    ]
    per_day_counts = distribute_counts(config.total_payments, weights)

    payment_ids = _IdSequence("PAY")
    fee_ids = _IdSequence("FEE")
    tax_ids = _IdSequence("TAX")

    payments: list[Payment] = []
    fee_lines: list[FeeLine] = []
    tax_lines: list[TaxLine] = []

    for day, count in zip(days, per_day_counts):
        batch_id = settlement_batch_id(day)
        for _ in range(count):
            method = sample_method(rng, config.method_weights)
            card_type: CardType | None = None
            network = None
            is_international = False
            if method is PaymentMethod.CARD:
                card_type = sample_card_type(rng, config.card_type_weights)
                is_international = rng.random() < config.international_rate_of_cards
                network = sample_network(rng, is_international)

            amount_paise = sample_amount_paise(
                rng, config.lognormal_mu, config.lognormal_sigma, config.amount_floor_paise, config.amount_cap_paise
            )
            captured_at = ist_moment(day, rng, config.business_hours)

            payment = Payment(
                id=payment_ids.next(),
                merchant_id=config.merchant_id,
                amount=Money(amount_paise),
                method=method,
                network=network,
                card_type=card_type,
                is_international=is_international,
                mcc=config.mcc,
                captured_at=captured_at,
                settlement_id=batch_id,
            )
            payments.append(payment)

            p_fees, p_taxes = _compute_fee_and_tax_lines(payment, rate_card, fee_ids, tax_ids)
            fee_lines.extend(p_fees)
            tax_lines.extend(p_taxes)

    return payments, fee_lines, tax_lines


def _generate_refunds(config: GenerationConfig, payments: list[Payment], rng: Random) -> tuple[list[Refund], set[str]]:
    _, last_day = month_bounds(config.month)
    count = round(len(payments) * config.refund_rate)
    chosen = rng.sample(payments, k=min(count, len(payments)))
    refund_ids = _IdSequence("REF")
    refunds: list[Refund] = []
    for payment in chosen:
        is_partial = rng.random() < config.refund_partial_rate
        if is_partial:
            lo, hi = config.refund_partial_fraction_range
            fraction = rng.uniform(lo, hi)
            amount_paise = max(1, min(payment.amount.paise - 1, round(payment.amount.paise * fraction)))
        else:
            amount_paise = payment.amount.paise

        lag_days = rng.randint(*config.refund_lag_days)
        # Clamped to the month's last day rather than left to spill into a
        # fictitious next month: an unclamped tail day would have zero
        # underlying payment gross to offset the refund deduction, which
        # produced spuriously negative batch credits (see DECISIONS.md).
        created_day = min(payment.captured_at.date() + timedelta(days=lag_days), last_day)
        created_at = ist_moment(created_day, rng, config.business_hours)

        refunds.append(
            Refund(
                id=refund_ids.next(),
                payment_id=payment.id,
                amount=Money(amount_paise),
                is_partial=is_partial,
                created_at=created_at,
                settlement_id=settlement_batch_id(created_day),
            )
        )
    return refunds, {p.id for p in chosen}


def _generate_chargebacks(
    config: GenerationConfig, payments: list[Payment], excluded_payment_ids: set[str], rng: Random
) -> tuple[list[Chargeback], list[Adjustment]]:
    _, last_day = month_bounds(config.month)
    eligible = [p for p in payments if p.id not in excluded_payment_ids]
    count = round(len(payments) * config.chargeback_rate)
    chosen = rng.sample(eligible, k=min(count, len(eligible)))

    chargeback_ids = _IdSequence("CB")
    reversal_ids = _IdSequence("ADJ-CBR")
    chargebacks: list[Chargeback] = []
    reversal_adjustments: list[Adjustment] = []

    for payment in chosen:
        reason_code = rng.choice(config.chargeback_reason_codes)
        raised_lag = rng.randint(*config.chargeback_raised_lag_days)
        # Clamped for the same reason as refunds above: an unclamped tail
        # day has no underlying payment gross to offset the deduction.
        raised_day = min(payment.captured_at.date() + timedelta(days=raised_lag), last_day)
        raised_at = ist_moment(raised_day, rng, config.business_hours)

        outcome_roll = rng.random()
        resolution_lag = rng.randint(*config.chargeback_resolution_lag_days)
        resolved_day = min(raised_day + timedelta(days=resolution_lag), last_day)
        resolved_at = ist_moment(resolved_day, rng, config.business_hours)

        if outcome_roll < config.chargeback_won_rate:
            stage = ChargebackStage.WON
        elif outcome_roll < config.chargeback_won_rate + config.chargeback_lost_rate:
            stage = ChargebackStage.LOST
        else:
            stage = ChargebackStage.RAISED
            resolved_at = None

        chargeback = Chargeback(
            id=chargeback_ids.next(),
            payment_id=payment.id,
            amount=payment.amount,
            reason_code=reason_code,
            stage=stage,
            raised_at=raised_at,
            resolved_at=resolved_at,
            settlement_id=settlement_batch_id(raised_day),
        )
        chargebacks.append(chargeback)

        if stage == ChargebackStage.WON:
            reversal_adjustments.append(
                Adjustment(
                    id=reversal_ids.next(),
                    kind=AdjustmentKind.MANUAL_CREDIT,
                    amount=chargeback.amount,
                    reason=f"chargeback reversal for {chargeback.id}",
                    created_at=resolved_at,
                    settlement_id=settlement_batch_id(resolved_day),
                )
            )

    return chargebacks, reversal_adjustments


def _generate_reserve_and_goodwill_adjustments(
    config: GenerationConfig, payments: list[Payment], rng: Random
) -> list[Adjustment]:
    _, last_day = month_bounds(config.month)

    gross_by_day: dict[date, int] = defaultdict(int)
    for p in payments:
        gross_by_day[p.captured_at.date()] += p.amount.paise

    hold_ids = _IdSequence("ADJ-RH")
    release_ids = _IdSequence("ADJ-RR")
    goodwill_ids = _IdSequence("ADJ-GW")
    adjustments: list[Adjustment] = []

    for day, gross in sorted(gross_by_day.items()):
        batch_id = settlement_batch_id(day)
        hold_amount = round(gross * config.reserve_hold_bps / 10_000)
        if hold_amount <= 0:
            continue
        hold_at = ist_moment(day, rng, config.business_hours)
        adjustments.append(
            Adjustment(
                id=hold_ids.next(),
                kind=AdjustmentKind.RESERVE_HOLD,
                amount=Money(hold_amount),
                reason=f"reserve hold on {batch_id}",
                created_at=hold_at,
                settlement_id=batch_id,
            )
        )

        release_day = day + timedelta(days=config.reserve_hold_release_days)
        if release_day <= last_day:
            release_at = ist_moment(release_day, rng, config.business_hours)
            adjustments.append(
                Adjustment(
                    id=release_ids.next(),
                    kind=AdjustmentKind.RESERVE_RELEASE,
                    amount=Money(hold_amount),
                    reason=f"reserve release for hold on {batch_id}",
                    created_at=release_at,
                    settlement_id=settlement_batch_id(release_day),
                )
            )

    goodwill_count = round(len(payments) * config.goodwill_credit_rate)
    first_day, _ = month_bounds(config.month)
    span_days = (last_day - first_day).days + 1
    for _ in range(goodwill_count):
        day = first_day + timedelta(days=rng.randrange(span_days))
        lo, hi = config.goodwill_credit_range_paise
        amount = rng.randint(lo, hi)
        moment = ist_moment(day, rng, config.business_hours)
        adjustments.append(
            Adjustment(
                id=goodwill_ids.next(),
                kind=AdjustmentKind.MANUAL_CREDIT,
                amount=Money(amount),
                reason="goodwill credit",
                created_at=moment,
                settlement_id=settlement_batch_id(day),
            )
        )

    return adjustments


def generate_narration(rng: Random, utr: str, batch_id: str) -> str:
    """Baseline BankCredit narration noise: inconsistent casing, bank
    prefixes, double spaces, and (~5% of the time) a truncated-looking UTR
    fragment embedded in the free-text narration. This never touches the
    structured BankCredit.utr field -- that is a separate, deliberate
    corruption datagen/inject.py applies for D09, at a configurable count,
    not baseline noise on every run."""
    prefix = rng.choice(_BANK_PREFIXES)
    # "Settlement" (not "SETTLEMENT") so the unmodified branch below is
    # genuinely mixed-case against the all-caps prefix/batch_id/utr --
    # otherwise "unmodified" and "forced upper" would be indistinguishable.
    text = f"{prefix}Settlement {batch_id} {utr}"

    casing_roll = rng.random()
    if casing_roll < 0.4:
        text = text.upper()
    elif casing_roll < 0.6:
        text = text.lower()

    if rng.random() < 0.3:
        text = text.replace(" ", "  ", 1)

    if rng.random() < 0.05:
        text = f"{text} REF{utr[:6]}"

    return text


def compute_batch_net_paise(world: World, batch_id: str) -> int:
    """The settlement net for one batch, recomputed directly from the raw
    records currently in `world`. The single implementation of the
    conservation formula in the codebase: the initial true-world assembly
    below and datagen/inject.py's post-mutation recomputation both call
    this rather than each keeping their own copy of the arithmetic."""
    payment_ids = {p.id for p in world.payments if p.settlement_id == batch_id}
    gross = sum(p.amount.paise for p in world.payments if p.settlement_id == batch_id)

    fee_lines_in_batch = [f for f in world.fee_lines if f.applies_to_id in payment_ids]
    fees = sum(f.computed_amount.paise for f in fee_lines_in_batch)
    fee_ids_in_batch = {f.id for f in fee_lines_in_batch}

    tax = sum(t.amount.paise for t in world.tax_lines if t.applies_to_fee_id in fee_ids_in_batch)
    refunds_total = sum(r.amount.paise for r in world.refunds if r.settlement_id == batch_id)
    chargebacks_total = sum(c.amount.paise for c in world.chargebacks if c.settlement_id == batch_id)
    add_back = sum(
        a.amount.paise for a in world.adjustments if a.settlement_id == batch_id and a.kind in _ADD_BACK_KINDS
    )
    subtract = sum(
        a.amount.paise for a in world.adjustments if a.settlement_id == batch_id and a.kind in _SUBTRACT_KINDS
    )

    return gross - refunds_total - fees - tax - chargebacks_total + add_back - subtract


def _assemble_batches(config: GenerationConfig, world: World, rng: Random) -> tuple[list[SettlementBatch], list[BankCredit]]:
    batch_ids: set[str] = set()
    batch_ids.update(p.settlement_id for p in world.payments)
    batch_ids.update(r.settlement_id for r in world.refunds)
    batch_ids.update(c.settlement_id for c in world.chargebacks)
    batch_ids.update(a.settlement_id for a in world.adjustments)

    batches: list[SettlementBatch] = []
    bank_credits: list[BankCredit] = []
    bank_credit_ids = _IdSequence("BC")

    for batch_id in sorted(batch_ids):
        day = date(int(batch_id[4:8]), int(batch_id[8:10]), int(batch_id[10:12]))
        net = compute_batch_net_paise(world, batch_id)

        utr = f"HDFC0001234{batch_id[4:]}{'0' * 6}"
        batches.append(
            SettlementBatch(
                id=batch_id,
                merchant_id=config.merchant_id,
                cycle_start=datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=IST),
                cycle_end=datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=IST),
                expected_credit=Money(net),
                utr=utr,
                status=BatchStatus.SETTLED,
            )
        )

        value_date = credit_value_date(day, config.settlement_lag_days)
        narration = generate_narration(rng, utr, batch_id)
        bank_credits.append(
            BankCredit(
                id=bank_credit_ids.next(),
                utr=utr,
                amount=Money(net),
                value_date=value_date,
                narration=narration,
            )
        )

    return batches, bank_credits


def build_true_world(config: GenerationConfig, rate_card: RateCard, rng: Random) -> World:
    payments, fee_lines, tax_lines = _generate_payments(config, rate_card, rng)
    refunds, refunded_payment_ids = _generate_refunds(config, payments, rng)
    chargebacks, reversal_adjustments = _generate_chargebacks(config, payments, refunded_payment_ids, rng)
    reserve_and_goodwill = _generate_reserve_and_goodwill_adjustments(config, payments, rng)
    adjustments = reversal_adjustments + reserve_and_goodwill

    world = World(
        payments=payments,
        refunds=refunds,
        chargebacks=chargebacks,
        adjustments=adjustments,
        fee_lines=fee_lines,
        tax_lines=tax_lines,
        batches=[],
        bank_credits=[],
    )
    world.batches, world.bank_credits = _assemble_batches(config, world, rng)
    return world
