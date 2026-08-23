"""Plants D01-D12 discrepancies into a copy of the true world, producing a
"reported" world (what the gateway's settlement report + bank statement
actually show) and a list of ground-truth entries recording exactly what
changed and by how much.

Injection order is fixed (D01..D12, then silent corruption) so a given
seed always plants the same instances regardless of iteration order
elsewhere. Each injector draws only from records untouched by an earlier
class in this run (via _TouchTracker), so no record carries two
discrepancies and every ground-truth entry stays cleanly attributable to
one cause.

Every D0x mapping below reuses the EXISTING core.exceptions.DiscrepancyClass
taxonomy -- no new enum member is added. See DECISIONS.md for the mapping
rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN
from random import Random

from core.exceptions import DiscrepancyClass
from core.models import (
    AdjustmentKind,
    CardType,
    ChargebackStage,
    EntityType,
    FeeLine,
    FeeType,
    PaymentMethod,
    RecordRef,
    TaxLine,
)
from core.money import Money
from datagen.ground_truth import DataQualityFlag, DiscrepancyEntry, SilentCorruption
from datagen.ratecard import RateCard, gst_amount, mdr_amount, mdr_raw_amount
from datagen.world import World, compute_batch_net_paise


class InjectionProfileError(Exception):
    """Raised when a requested injection count exceeds the natural eligible
    population for that discrepancy class -- never silently under-delivered."""


@dataclass
class _TouchTracker:
    payments: set[str] = field(default_factory=set)
    refunds: set[str] = field(default_factory=set)
    chargebacks: set[str] = field(default_factory=set)
    bank_credits: set[str] = field(default_factory=set)


class _IdSequence:
    def __init__(self, prefix: str, width: int = 6):
        self._prefix = prefix
        self._width = width
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"{self._prefix}-{self._n:0{self._width}d}"


def _find(items: list, item_id: str):
    for x in items:
        if x.id == item_id:
            return x
    raise KeyError(item_id)


def _replace(items: list, item_id: str, updated) -> None:
    for i, x in enumerate(items):
        if x.id == item_id:
            items[i] = updated
            return
    raise KeyError(item_id)


@dataclass
class _CreditBook:
    """Everything an injector needs to write a batch's expected_credit and
    its paired BankCredit.amount, threaded through the whole run.

    `credit_id_by_batch` is resolved ONCE from the original (pre-injection)
    1:1 utr pairing. Injectors must use this fixed mapping rather than
    re-matching by utr afterward: D09 deliberately corrupts BankCredit.utr,
    which would silently break a utr-based lookup for any injector that
    touches the same batch later in the fixed D01..D12 order.

    `unbacked_delta_by_batch` accumulates the per-batch net delta that is
    NOT derivable from the record set (D04 only -- see
    _adjust_batch_and_credit). compute_batch_net_paise re-derives a batch
    purely from its records, so a later injector recomputing the same batch
    would silently erase those deltas unless every recompute re-applies
    them. This is the same ordering hazard as the utr one above: D04 runs
    4th, and D05/D06/D07/D08/D10/D11/D12 all recompute batches after it."""

    credit_id_by_batch: dict[str, str]
    unbacked_delta_by_batch: dict[str, int] = field(default_factory=dict)


def _build_credit_book(world: World) -> _CreditBook:
    utr_to_credit_id = {bc.utr: bc.id for bc in world.bank_credits}
    return _CreditBook(credit_id_by_batch={batch.id: utr_to_credit_id[batch.utr] for batch in world.batches})


def _set_batch_and_credit(world: World, credits: _CreditBook, batch_id: str, new_net: int) -> None:
    batch = _find(world.batches, batch_id)
    _replace(world.batches, batch_id, batch.model_copy(update={"expected_credit": Money(new_net)}))
    credit_id = credits.credit_id_by_batch[batch_id]
    credit = _find(world.bank_credits, credit_id)
    _replace(world.bank_credits, credit_id, credit.model_copy(update={"amount": Money(new_net)}))


def _recompute_batch_and_credit(world: World, credits: _CreditBook, batch_id: str) -> None:
    """For discrepancies backed by an actual added/removed/changed source
    record (fee/tax/adjustment/settlement_id changes) -- compute_batch_net_
    paise re-derives correctly from the current record set, plus any
    already-planted unbacked delta this batch is carrying, which by
    definition no record can reproduce."""
    net = compute_batch_net_paise(world, batch_id) + credits.unbacked_delta_by_batch.get(batch_id, 0)
    _set_batch_and_credit(world, credits, batch_id, net)


def _adjust_batch_and_credit(world: World, credits: _CreditBook, batch_id: str, delta_paise: int) -> None:
    """For discrepancies NOT backed by any source-record change (D04: the
    ledger's Refund list stays exactly as it was -- only the settlement's
    arithmetic is wrong). Re-deriving from records would not show this, so
    the delta is also banked on the batch to survive any later recompute."""
    credits.unbacked_delta_by_batch[batch_id] = credits.unbacked_delta_by_batch.get(batch_id, 0) + delta_paise
    batch = _find(world.batches, batch_id)
    _set_batch_and_credit(world, credits, batch_id, batch.expected_credit.paise + delta_paise)


# ---------------------------------------------------------------------------
# D01 -- wrong MDR tier applied
# ---------------------------------------------------------------------------


def _inject_d01(world, rate_card, count, rng, touch, credits):
    eligible = [p for p in world.payments if p.method == PaymentMethod.CARD and p.id not in touch.payments]
    rng.shuffle(eligible)

    entries: list[DiscrepancyEntry] = []
    for payment in eligible:
        if len(entries) >= count:
            break
        version = rate_card.version_for(payment.captured_at)
        rule = version.rule_for(payment.method, payment.card_type)
        correct_tier = rule.tier_for(payment.amount.paise)
        alternatives = [t for t in rule.tiers if t is not correct_tier]
        if not alternatives:
            continue
        wrong_tier = rng.choice(alternatives)
        wrong_mdr = mdr_amount(payment.amount.paise, wrong_tier)

        fee_line = next(f for f in world.fee_lines if f.applies_to_id == payment.id and f.fee_type == FeeType.MDR)
        if wrong_mdr == fee_line.computed_amount.paise:
            continue
        delta_fee = wrong_mdr - fee_line.computed_amount.paise
        _replace(world.fee_lines, fee_line.id, fee_line.model_copy(update={"computed_amount": Money(wrong_mdr)}))

        tax_line = next(t for t in world.tax_lines if t.applies_to_fee_id == fee_line.id)
        new_tax = gst_amount(wrong_mdr, version.gst_bps)
        delta_tax = new_tax - tax_line.amount.paise
        _replace(
            world.tax_lines,
            tax_line.id,
            tax_line.model_copy(update={"base_amount": Money(wrong_mdr), "amount": Money(new_tax)}),
        )

        _recompute_batch_and_credit(world, credits, payment.settlement_id)

        total_delta = delta_fee + delta_tax
        discrepancy_class = DiscrepancyClass.FEE_UNDERCHARGE if total_delta < 0 else DiscrepancyClass.FEE_OVERCHARGE
        entries.append(
            DiscrepancyEntry(
                code="D01",
                discrepancy_class=discrepancy_class,
                records=[
                    RecordRef(type=EntityType.FEE_LINE, id=fee_line.id),
                    RecordRef(type=EntityType.PAYMENT, id=payment.id),
                ],
                amount_impact_paise=abs(total_delta),
                detail={
                    "correct_tier_bps": correct_tier.bps,
                    "wrong_tier_bps": wrong_tier.bps,
                    "payment_gross_paise": payment.amount.paise,
                },
            )
        )
        touch.payments.add(payment.id)

    if len(entries) < count:
        raise InjectionProfileError(f"D01: requested {count}, only {len(entries)} eligible CARD payments available")
    return entries


# ---------------------------------------------------------------------------
# D02 -- tax computed on the wrong base (gross instead of fee)
# ---------------------------------------------------------------------------


def _inject_d02(world, rate_card, count, rng, touch, credits):
    eligible_fee_lines = [f for f in world.fee_lines if f.applies_to_id not in touch.payments]
    rng.shuffle(eligible_fee_lines)

    entries: list[DiscrepancyEntry] = []
    for fee_line in eligible_fee_lines:
        if len(entries) >= count:
            break
        if fee_line.applies_to_id in touch.payments:
            # eligible_fee_lines is a snapshot taken before this loop --
            # a payment with two fee lines (NETBANKING/WALLET carry both
            # MDR and FIXED) can appear twice in it, so re-check here
            # rather than only at snapshot time, or the second fee line
            # would get its own D02 entry on an already-touched payment.
            continue
        payment = next(p for p in world.payments if p.id == fee_line.applies_to_id)
        version = rate_card.version_for(payment.captured_at)
        tax_line = next(t for t in world.tax_lines if t.applies_to_fee_id == fee_line.id)

        wrong_base = payment.amount.paise
        wrong_tax = gst_amount(wrong_base, version.gst_bps)
        if wrong_tax == tax_line.amount.paise:
            continue
        delta = wrong_tax - tax_line.amount.paise
        _replace(
            world.tax_lines,
            tax_line.id,
            tax_line.model_copy(update={"base_amount": Money(wrong_base), "amount": Money(wrong_tax)}),
        )

        _recompute_batch_and_credit(world, credits, payment.settlement_id)

        entries.append(
            DiscrepancyEntry(
                code="D02",
                discrepancy_class=DiscrepancyClass.TAX_MISCALCULATION,
                records=[
                    RecordRef(type=EntityType.TAX_LINE, id=tax_line.id),
                    RecordRef(type=EntityType.PAYMENT, id=payment.id),
                ],
                amount_impact_paise=abs(delta),
                detail={
                    "correct_base_paise": tax_line.base_amount.paise,
                    "wrong_base_paise": wrong_base,
                    "gst_bps": version.gst_bps,
                },
            )
        )
        touch.payments.add(payment.id)

    if len(entries) < count:
        raise InjectionProfileError(f"D02: requested {count}, only {len(entries)} eligible fee lines available")
    return entries


# ---------------------------------------------------------------------------
# D03 -- fee cap not applied where the contract requires it
# ---------------------------------------------------------------------------


def _inject_d03(world, rate_card, count, rng, touch, credits):
    candidates = []
    for payment in world.payments:
        if payment.method != PaymentMethod.CARD or payment.card_type != CardType.DEBIT:
            continue
        if payment.id in touch.payments:
            continue
        version = rate_card.version_for(payment.captured_at)
        rule = version.rule_for(payment.method, payment.card_type)
        tier = rule.tier_for(payment.amount.paise)
        if tier.cap_paise is None:
            continue
        uncapped = mdr_raw_amount(payment.amount.paise, tier.bps)
        if uncapped <= tier.cap_paise:
            continue  # cap wasn't actually binding for this payment
        candidates.append((payment, version, tier, uncapped))
    rng.shuffle(candidates)

    entries: list[DiscrepancyEntry] = []
    for payment, version, tier, uncapped in candidates:
        if len(entries) >= count:
            break
        fee_line = next(f for f in world.fee_lines if f.applies_to_id == payment.id and f.fee_type == FeeType.MDR)
        delta_fee = uncapped - fee_line.computed_amount.paise
        _replace(world.fee_lines, fee_line.id, fee_line.model_copy(update={"computed_amount": Money(uncapped)}))

        tax_line = next(t for t in world.tax_lines if t.applies_to_fee_id == fee_line.id)
        new_tax = gst_amount(uncapped, version.gst_bps)
        delta_tax = new_tax - tax_line.amount.paise
        _replace(
            world.tax_lines,
            tax_line.id,
            tax_line.model_copy(update={"base_amount": Money(uncapped), "amount": Money(new_tax)}),
        )

        _recompute_batch_and_credit(world, credits, payment.settlement_id)

        entries.append(
            DiscrepancyEntry(
                code="D03",
                discrepancy_class=DiscrepancyClass.FEE_OVERCHARGE,
                records=[
                    RecordRef(type=EntityType.FEE_LINE, id=fee_line.id),
                    RecordRef(type=EntityType.PAYMENT, id=payment.id),
                ],
                amount_impact_paise=delta_fee + delta_tax,
                detail={"cap_paise": tier.cap_paise, "uncapped_paise": uncapped},
            )
        )
        touch.payments.add(payment.id)

    if len(entries) < count:
        raise InjectionProfileError(f"D03: requested {count}, only {len(entries)} eligible debit payments available")
    return entries


# ---------------------------------------------------------------------------
# D04 -- refund deducted twice (settlement-side only; the ledger's Refund
# list is never duplicated)
# ---------------------------------------------------------------------------


def _inject_d04(world, count, rng, touch, credits):
    eligible = [r for r in world.refunds if r.id not in touch.refunds]
    rng.shuffle(eligible)

    entries: list[DiscrepancyEntry] = []
    for refund in eligible[:count]:
        _adjust_batch_and_credit(world, credits, refund.settlement_id, -refund.amount.paise)
        entries.append(
            DiscrepancyEntry(
                code="D04",
                discrepancy_class=DiscrepancyClass.REFUND_AMOUNT_MISMATCH,
                records=[
                    RecordRef(type=EntityType.REFUND, id=refund.id),
                    RecordRef(type=EntityType.SETTLEMENT_BATCH, id=refund.settlement_id),
                ],
                amount_impact_paise=refund.amount.paise,
                detail={"batch_id": refund.settlement_id},
            )
        )
        touch.refunds.add(refund.id)

    if len(entries) < count:
        raise InjectionProfileError(f"D04: requested {count}, only {len(entries)} eligible refunds available")
    return entries


# ---------------------------------------------------------------------------
# D05 -- chargeback won but the reversal never credited back
# ---------------------------------------------------------------------------


def _inject_d05(world, count, rng, touch, credits):
    won = [c for c in world.chargebacks if c.stage == ChargebackStage.WON and c.id not in touch.chargebacks]
    rng.shuffle(won)

    entries: list[DiscrepancyEntry] = []
    for chargeback in won:
        if len(entries) >= count:
            break
        reversal = next(
            (a for a in world.adjustments if a.kind == AdjustmentKind.MANUAL_CREDIT and chargeback.id in a.reason),
            None,
        )
        if reversal is None:
            continue
        world.adjustments[:] = [a for a in world.adjustments if a.id != reversal.id]
        _recompute_batch_and_credit(world, credits, reversal.settlement_id)

        entries.append(
            DiscrepancyEntry(
                code="D05",
                discrepancy_class=DiscrepancyClass.CHARGEBACK_AMOUNT_MISMATCH,
                records=[RecordRef(type=EntityType.CHARGEBACK, id=chargeback.id)],
                amount_impact_paise=reversal.amount.paise,
                detail={"chargeback_stage": chargeback.stage.value, "omitted_reversal_id": reversal.id},
            )
        )
        touch.chargebacks.add(chargeback.id)

    if len(entries) < count:
        raise InjectionProfileError(f"D05: requested {count}, only {len(entries)} won chargebacks with a reversal available")
    return entries


# ---------------------------------------------------------------------------
# D06 -- refund deducted in a cycle where it was not due
# ---------------------------------------------------------------------------


def _inject_d06(world, count, rng, touch, credits):
    eligible = [r for r in world.refunds if r.id not in touch.refunds]
    rng.shuffle(eligible)
    all_batch_ids = [b.id for b in world.batches]

    entries: list[DiscrepancyEntry] = []
    for refund in eligible:
        if len(entries) >= count:
            break
        other_batches = [b for b in all_batch_ids if b != refund.settlement_id]
        if not other_batches:
            continue
        wrong_batch_id = rng.choice(other_batches)
        true_batch_id = refund.settlement_id

        _replace(world.refunds, refund.id, refund.model_copy(update={"settlement_id": wrong_batch_id}))
        _recompute_batch_and_credit(world, credits, true_batch_id)
        _recompute_batch_and_credit(world, credits, wrong_batch_id)

        entries.append(
            DiscrepancyEntry(
                code="D06",
                discrepancy_class=DiscrepancyClass.REFUND_AMOUNT_MISMATCH,
                records=[
                    RecordRef(type=EntityType.REFUND, id=refund.id),
                    RecordRef(type=EntityType.SETTLEMENT_BATCH, id=true_batch_id),
                    RecordRef(type=EntityType.SETTLEMENT_BATCH, id=wrong_batch_id),
                ],
                amount_impact_paise=refund.amount.paise,
                detail={"true_batch_id": true_batch_id, "wrong_batch_id": wrong_batch_id},
            )
        )
        touch.refunds.add(refund.id)

    if len(entries) < count:
        raise InjectionProfileError(f"D06: requested {count}, only {len(entries)} eligible refunds available")
    return entries


# ---------------------------------------------------------------------------
# D07 -- rounding drift (half-up vs half-even applied inconsistently)
# ---------------------------------------------------------------------------


def _inject_d07(world, count, rng, touch, credits):
    candidates = []
    for tax_line in world.tax_lines:
        fee_line = next(f for f in world.fee_lines if f.id == tax_line.applies_to_fee_id)
        if fee_line.applies_to_id in touch.payments:
            continue
        half_even = gst_amount(tax_line.base_amount.paise, tax_line.rate_bps, rounding=ROUND_HALF_EVEN)
        if half_even != tax_line.amount.paise:
            candidates.append((fee_line.applies_to_id, tax_line, half_even))
    rng.shuffle(candidates)

    entries: list[DiscrepancyEntry] = []
    for payment_id, tax_line, half_even in candidates:
        if len(entries) >= count:
            break
        delta = half_even - tax_line.amount.paise
        _replace(world.tax_lines, tax_line.id, tax_line.model_copy(update={"amount": Money(half_even)}))

        payment = next(p for p in world.payments if p.id == payment_id)
        _recompute_batch_and_credit(world, credits, payment.settlement_id)

        entries.append(
            DiscrepancyEntry(
                code="D07",
                discrepancy_class=DiscrepancyClass.ROUNDING_DRIFT,
                records=[RecordRef(type=EntityType.TAX_LINE, id=tax_line.id)],
                amount_impact_paise=abs(delta),
                detail={"half_up_paise": tax_line.amount.paise, "half_even_paise": half_even},
            )
        )
        touch.payments.add(payment_id)

    if len(entries) < count:
        raise InjectionProfileError(f"D07: requested {count}, only {len(entries)} half-paise-tie candidates found")
    return entries


# ---------------------------------------------------------------------------
# D08 -- captured payment never appears in any settlement
# ---------------------------------------------------------------------------


def _inject_d08(world, count, rng, touch, credits):
    eligible = [p for p in world.payments if p.id not in touch.payments]
    rng.shuffle(eligible)

    entries: list[DiscrepancyEntry] = []
    for payment in eligible[:count]:
        batch_id = payment.settlement_id
        related_fee_ids = {f.id for f in world.fee_lines if f.applies_to_id == payment.id}
        true_contribution = (
            payment.amount.paise
            - sum(f.computed_amount.paise for f in world.fee_lines if f.id in related_fee_ids)
            - sum(t.amount.paise for t in world.tax_lines if t.applies_to_fee_id in related_fee_ids)
        )

        _replace(world.payments, payment.id, payment.model_copy(update={"settlement_id": None}))
        world.fee_lines[:] = [f for f in world.fee_lines if f.id not in related_fee_ids]
        world.tax_lines[:] = [t for t in world.tax_lines if t.applies_to_fee_id not in related_fee_ids]

        _recompute_batch_and_credit(world, credits, batch_id)

        entries.append(
            DiscrepancyEntry(
                code="D08",
                discrepancy_class=DiscrepancyClass.MISSING_TRANSACTION,
                records=[RecordRef(type=EntityType.PAYMENT, id=payment.id)],
                amount_impact_paise=true_contribution,
                detail={"true_batch_id": batch_id, "payment_gross_paise": payment.amount.paise},
            )
        )
        touch.payments.add(payment.id)

    if len(entries) < count:
        raise InjectionProfileError(f"D08: requested {count}, only {len(entries)} eligible payments available")
    return entries


# ---------------------------------------------------------------------------
# D09 -- corrupted/truncated UTR: data quality only, zero rupee impact
# ---------------------------------------------------------------------------


def _inject_d09(world, count, rng, touch):
    eligible = [bc for bc in world.bank_credits if bc.id not in touch.bank_credits]
    rng.shuffle(eligible)

    flags: list[DataQualityFlag] = []
    for bank_credit in eligible[:count]:
        true_utr = bank_credit.utr
        cut = rng.randint(6, max(6, len(true_utr) - 4))
        corrupted_utr = true_utr[:cut]
        _replace(world.bank_credits, bank_credit.id, bank_credit.model_copy(update={"utr": corrupted_utr}))

        flags.append(
            DataQualityFlag(
                code="D09",
                records=[RecordRef(type=EntityType.BANK_CREDIT, id=bank_credit.id)],
                amount_impact_paise=0,
                detail={"field": "utr", "true_utr": true_utr, "corrupted_utr": corrupted_utr},
            )
        )
        touch.bank_credits.add(bank_credit.id)

    if len(flags) < count:
        raise InjectionProfileError(f"D09: requested {count}, only {len(flags)} eligible bank credits available")
    return flags


# ---------------------------------------------------------------------------
# D10 -- stale contract version applied after the revision's effective date
# ---------------------------------------------------------------------------


def _inject_d10(world, rate_card, count, rng, touch, credits):
    v1, v2 = sorted(rate_card.versions, key=lambda v: v.effective_from)

    candidates = []
    for payment in world.payments:
        if payment.method != PaymentMethod.CARD or payment.id in touch.payments:
            continue
        if rate_card.version_for(payment.captured_at).version_id != v2.version_id:
            continue  # predates the revision; nothing "stale" to apply
        candidates.append(payment)
    rng.shuffle(candidates)

    entries: list[DiscrepancyEntry] = []
    for payment in candidates:
        if len(entries) >= count:
            break
        stale_rule = v1.rule_for(payment.method, payment.card_type)
        stale_tier = stale_rule.tier_for(payment.amount.paise)
        stale_mdr = mdr_amount(payment.amount.paise, stale_tier)

        fee_line = next(f for f in world.fee_lines if f.applies_to_id == payment.id and f.fee_type == FeeType.MDR)
        if stale_mdr == fee_line.computed_amount.paise:
            continue
        delta_fee = stale_mdr - fee_line.computed_amount.paise
        _replace(world.fee_lines, fee_line.id, fee_line.model_copy(update={"computed_amount": Money(stale_mdr)}))

        tax_line = next(t for t in world.tax_lines if t.applies_to_fee_id == fee_line.id)
        new_tax = gst_amount(stale_mdr, v2.gst_bps)
        delta_tax = new_tax - tax_line.amount.paise
        _replace(
            world.tax_lines,
            tax_line.id,
            tax_line.model_copy(update={"base_amount": Money(stale_mdr), "amount": Money(new_tax)}),
        )

        _recompute_batch_and_credit(world, credits, payment.settlement_id)

        total_delta = delta_fee + delta_tax
        discrepancy_class = DiscrepancyClass.FEE_UNDERCHARGE if total_delta < 0 else DiscrepancyClass.FEE_OVERCHARGE
        entries.append(
            DiscrepancyEntry(
                code="D10",
                discrepancy_class=discrepancy_class,
                records=[
                    RecordRef(type=EntityType.FEE_LINE, id=fee_line.id),
                    RecordRef(type=EntityType.PAYMENT, id=payment.id),
                ],
                amount_impact_paise=abs(total_delta),
                detail={"stale_version": v1.version_id, "correct_version": v2.version_id, "stale_bps": stale_tier.bps},
            )
        )
        touch.payments.add(payment.id)

    if len(entries) < count:
        raise InjectionProfileError(f"D10: requested {count}, only {len(entries)} eligible post-revision CARD payments available")
    return entries


# ---------------------------------------------------------------------------
# D11 -- fixed fee charged twice on one transaction
# ---------------------------------------------------------------------------


def _inject_d11(world, rate_card, count, rng, touch, fee_ids, tax_ids, credits):
    candidates = []
    for payment in world.payments:
        if payment.id in touch.payments:
            continue
        existing = next(
            (f for f in world.fee_lines if f.applies_to_id == payment.id and f.fee_type == FeeType.FIXED), None
        )
        if existing is not None:
            candidates.append((payment, existing))
    rng.shuffle(candidates)

    entries: list[DiscrepancyEntry] = []
    for payment, existing_fixed in candidates:
        if len(entries) >= count:
            break
        version = rate_card.version_for(payment.captured_at)
        dup_fee = FeeLine(
            id=fee_ids.next(),
            applies_to_id=payment.id,
            applies_to_type=EntityType.PAYMENT,
            fee_type=FeeType.FIXED,
            computed_amount=existing_fixed.computed_amount,
            rule_id=existing_fixed.rule_id,
        )
        world.fee_lines.append(dup_fee)
        dup_tax_amount = gst_amount(dup_fee.computed_amount.paise, version.gst_bps)
        dup_tax = TaxLine(
            id=tax_ids.next(),
            applies_to_fee_id=dup_fee.id,
            tax_type="GST",
            rate_bps=version.gst_bps,
            base_amount=dup_fee.computed_amount,
            amount=Money(dup_tax_amount),
        )
        world.tax_lines.append(dup_tax)

        _recompute_batch_and_credit(world, credits, payment.settlement_id)

        entries.append(
            DiscrepancyEntry(
                code="D11",
                discrepancy_class=DiscrepancyClass.FEE_OVERCHARGE,
                records=[
                    RecordRef(type=EntityType.FEE_LINE, id=dup_fee.id),
                    RecordRef(type=EntityType.PAYMENT, id=payment.id),
                ],
                amount_impact_paise=dup_fee.computed_amount.paise + dup_tax_amount,
                detail={"duplicated_fee_line_id": existing_fixed.id, "fixed_fee_paise": dup_fee.computed_amount.paise},
            )
        )
        touch.payments.add(payment.id)

    if len(entries) < count:
        raise InjectionProfileError(f"D11: requested {count}, only {len(entries)} eligible payments with a fixed fee available")
    return entries


# ---------------------------------------------------------------------------
# D12 -- international surcharge applied to a domestic transaction
# ---------------------------------------------------------------------------


def _inject_d12(world, rate_card, count, rng, touch, fee_ids, tax_ids, credits):
    eligible = [
        p for p in world.payments if p.method == PaymentMethod.CARD and not p.is_international and p.id not in touch.payments
    ]
    rng.shuffle(eligible)

    entries: list[DiscrepancyEntry] = []
    for payment in eligible[:count]:
        version = rate_card.version_for(payment.captured_at)
        rule = version.rule_for(payment.method, payment.card_type)
        surcharge = mdr_raw_amount(payment.amount.paise, rule.international_surcharge_bps)

        spurious_fee = FeeLine(
            id=fee_ids.next(),
            applies_to_id=payment.id,
            applies_to_type=EntityType.PAYMENT,
            fee_type=FeeType.INTERNATIONAL,
            computed_amount=Money(surcharge),
            rule_id=f"{version.version_id}:international",
        )
        world.fee_lines.append(spurious_fee)
        spurious_tax_amount = gst_amount(surcharge, version.gst_bps)
        spurious_tax = TaxLine(
            id=tax_ids.next(),
            applies_to_fee_id=spurious_fee.id,
            tax_type="GST",
            rate_bps=version.gst_bps,
            base_amount=Money(surcharge),
            amount=Money(spurious_tax_amount),
        )
        world.tax_lines.append(spurious_tax)

        _recompute_batch_and_credit(world, credits, payment.settlement_id)

        entries.append(
            DiscrepancyEntry(
                code="D12",
                discrepancy_class=DiscrepancyClass.FEE_OVERCHARGE,
                records=[
                    RecordRef(type=EntityType.FEE_LINE, id=spurious_fee.id),
                    RecordRef(type=EntityType.PAYMENT, id=payment.id),
                ],
                amount_impact_paise=surcharge + spurious_tax_amount,
                detail={"surcharge_bps": rule.international_surcharge_bps, "payment_gross_paise": payment.amount.paise},
            )
        )
        touch.payments.add(payment.id)

    if len(entries) < count:
        raise InjectionProfileError(f"D12: requested {count}, only {len(entries)} eligible domestic CARD payments available")
    return entries


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def apply_discrepancies(
    true_world: World, rate_card: RateCard, profile, rng: Random
) -> tuple[World, list[DiscrepancyEntry], list[DataQualityFlag]]:
    world = true_world.copy()
    credits = _build_credit_book(world)
    touch = _TouchTracker()
    fee_ids = _IdSequence("FEE-INJ")
    tax_ids = _IdSequence("TAX-INJ")

    discrepancies: list[DiscrepancyEntry] = []
    discrepancies += _inject_d01(world, rate_card, profile.D01, rng, touch, credits)
    discrepancies += _inject_d02(world, rate_card, profile.D02, rng, touch, credits)
    discrepancies += _inject_d03(world, rate_card, profile.D03, rng, touch, credits)
    discrepancies += _inject_d04(world, profile.D04, rng, touch, credits)
    discrepancies += _inject_d05(world, profile.D05, rng, touch, credits)
    discrepancies += _inject_d06(world, profile.D06, rng, touch, credits)
    discrepancies += _inject_d07(world, profile.D07, rng, touch, credits)
    discrepancies += _inject_d08(world, profile.D08, rng, touch, credits)
    flags = _inject_d09(world, profile.D09, rng, touch)
    discrepancies += _inject_d10(world, rate_card, profile.D10, rng, touch, credits)
    discrepancies += _inject_d11(world, rate_card, profile.D11, rng, touch, fee_ids, tax_ids, credits)
    discrepancies += _inject_d12(world, rate_card, profile.D12, rng, touch, fee_ids, tax_ids, credits)

    return world, discrepancies, flags


# ---------------------------------------------------------------------------
# Silent corruption
# ---------------------------------------------------------------------------

_SILENT_CORRUPTION_TARGETS: tuple[tuple[EntityType, str, str], ...] = (
    (EntityType.PAYMENT, "payments", "amount"),
    (EntityType.REFUND, "refunds", "amount"),
    (EntityType.CHARGEBACK, "chargebacks", "amount"),
    (EntityType.ADJUSTMENT, "adjustments", "amount"),
    (EntityType.FEE_LINE, "fee_lines", "computed_amount"),
    (EntityType.TAX_LINE, "tax_lines", "base_amount"),
    (EntityType.TAX_LINE, "tax_lines", "amount"),
    (EntityType.SETTLEMENT_BATCH, "batches", "expected_credit"),
    (EntityType.BANK_CREDIT, "bank_credits", "amount"),
)


def apply_silent_corruption(world: World, rng: Random) -> tuple[World, SilentCorruption]:
    """Flips exactly one paise on exactly one Money-valued field on one
    uniformly chosen record across the whole world. Deliberately does NOT
    recompute any batch/bank-credit total afterward -- the entire point is
    an uncompensated, silent 1-paise discrepancy that only an independent
    recomputation (core/conserve.py, built later) can catch."""
    world = world.copy()

    candidates: list[tuple[EntityType, str, list, str]] = []
    for record_type, attr_name, field_name in _SILENT_CORRUPTION_TARGETS:
        items = getattr(world, attr_name)
        for item in items:
            candidates.append((record_type, item.id, items, field_name))

    record_type, record_id, items, field_name = rng.choice(candidates)
    record = _find(items, record_id)
    original: Money = getattr(record, field_name)

    sign = rng.choice((1, -1)) if original.paise > 0 else 1
    corrupted = Money(original.paise + sign)
    _replace(items, record_id, record.model_copy(update={field_name: corrupted}))

    return world, SilentCorruption(
        record_type=record_type.value,
        record_id=record_id,
        field=field_name,
        original_paise=original.paise,
        corrupted_paise=corrupted.paise,
        delta_paise=sign,
    )
