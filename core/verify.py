"""Independent recomputation + diff.

decompose.py asks "which records produced this credit". conserve.py asks
"does the settlement report's own arithmetic net to what it claims" -- an
identity check entirely within the report's own numbers. This module asks a
third, independent question: is that arithmetic *correct*, against the
contract. A report can be perfectly self-consistent (residual_paise == 0)
while every fee line was computed off the wrong MDR tier -- conserve.py is
structurally incapable of catching that, because it never looks at the
contract. Only re-deriving each fee and tax from CompiledContract.fee_for()
and diffing against what the ledger reports can.

Scope, deliberately narrow:
  - Fee/tax recomputation, aggregated by fee_type per payment (not 1:1 line
    matching -- this is what makes a duplicate fee line and a wrong-tier fee
    fall out of the same code path with no special-casing).
  - The one non-contract deterministic rule in scope: a WON chargeback needs
    a MANUAL_CREDIT reversal somewhere in the ledger (not necessarily this
    proof -- a reversal is booked on its resolution day, routinely a
    different settlement cycle, and therefore a different credit/proof,
    than the chargeback itself).
  - NOT in scope: MISSING_TRANSACTION and DUPLICATE_SETTLEMENT (run-level --
    need the union of every proof in a run, not one proof at a time).
  - NOT in scope: REFUND_AMOUNT_MISMATCH as a per-line check -- there is no
    independently recomputable "correct" refund amount in this data model
    to diff against (Refund is a single record, not a claimed-vs-true pair
    the way FeeLine/contract is). A refund-shaped discrepancy with no
    backing record change shows up only as conserve.py's unexplained_paise.
  - NOT in scope: fees attached to a non-Payment record (e.g. a dispute fee
    on a Chargeback) -- CompiledContract.fee_for() only prices Payments.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from core.contract import CompiledContract, FeeBreakdown
from core.decompose import DecompositionOutcome, DecompositionProof
from core.exceptions import DiscrepancyClass
from core.ledger import Ledger
from core.models import (
    AdjustmentKind,
    Chargeback,
    ChargebackStage,
    EntityType,
    FeeLine,
    FeeType,
    Finding,
    Lane,
    Payment,
    RecordRef,
    Severity,
    TaxLine,
)
from core.money import Money

# Exact deterministic recomputation, not a probabilistic match -- there is
# no ambiguity in a fee_for() call the way there is in decompose.py's
# subset-sum search, so there is nothing to hedge the way CONFIDENCE_BY_TIER does.
CONFIDENCE = 10_000


class UnsupportedTaxBase(Exception):
    """Raised for a TaxComponent with on_fee_type=None (a transaction-gross
    tax with no fee line to attach evidence to). TaxLine.applies_to_fee_id
    is required in core/models.py, so this shape has no committed
    representation; no rate card in this repo exercises it today. A loud,
    named failure here, rather than silently mismatching evidence, matches
    core/contract.py's NoApplicableRule discipline: never guess."""


def _resolved_payment_refs(proof: DecompositionProof) -> list[RecordRef]:
    return sorted({t.ref for t in proof.terms if t.ref.type is EntityType.PAYMENT}, key=lambda r: r.id)


def _reported_fee_totals(ledger: Ledger, payment_ref: RecordRef) -> dict[FeeType, int]:
    totals: dict[FeeType, int] = {}
    for fee_ref in ledger.fee_lines_for(payment_ref):
        fee_line: FeeLine = ledger.get(fee_ref)
        totals[fee_line.fee_type] = totals.get(fee_line.fee_type, 0) + fee_line.computed_amount.paise
    return totals


def _reported_tax_totals(ledger: Ledger, payment_ref: RecordRef) -> dict[FeeType, int]:
    totals: dict[FeeType, int] = {}
    for fee_ref in ledger.fee_lines_for(payment_ref):
        fee_line: FeeLine = ledger.get(fee_ref)
        for tax_ref in ledger.tax_lines_for(fee_ref):
            tax_line: TaxLine = ledger.get(tax_ref)
            totals[fee_line.fee_type] = totals.get(fee_line.fee_type, 0) + tax_line.amount.paise
    return totals


def _recomputed_fee_totals(breakdown: FeeBreakdown) -> dict[FeeType, int]:
    totals: dict[FeeType, int] = {}
    for component in breakdown.fees:
        totals[component.fee_type] = totals.get(component.fee_type, 0) + component.amount.paise
    return totals


def _recomputed_tax_totals(breakdown: FeeBreakdown) -> dict[FeeType, int]:
    totals: dict[FeeType, int] = {}
    for tax in breakdown.taxes:
        if tax.on_fee_type is None:
            raise UnsupportedTaxBase(
                f"payment {breakdown.payment_id}: a transaction-gross tax ({tax.tax_type}) has no "
                "fee line to attach evidence to -- verify.py does not support this shape yet"
            )
        totals[tax.on_fee_type] = totals.get(tax.on_fee_type, 0) + tax.amount.paise
    return totals


def _evidence_for_fee_type(ledger: Ledger, payment_ref: RecordRef, fee_type: FeeType) -> list[RecordRef]:
    return [payment_ref] + [
        ref for ref in ledger.fee_lines_for(payment_ref) if ledger.get(ref).fee_type is fee_type
    ]


def _evidence_for_tax_of_fee_type(ledger: Ledger, payment_ref: RecordRef, fee_type: FeeType) -> list[RecordRef]:
    refs = [payment_ref]
    for fee_ref in ledger.fee_lines_for(payment_ref):
        if ledger.get(fee_ref).fee_type is not fee_type:
            continue
        refs.append(fee_ref)
        refs.extend(ledger.tax_lines_for(fee_ref))
    return refs


class _IdSeq:
    """Deterministic, collision-free Finding ids: BankCredit ids are unique
    per Ledger, so <run>-<credit>-<seq> cannot collide across proofs, and
    every caller below iterates in a fixed sorted order, so a given proof
    produces the same ids on every replay."""

    def __init__(self, audit_run_id: str, credit_id: str):
        self._prefix = f"FND-{audit_run_id}-{credit_id}"
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"{self._prefix}-{self._n:03d}"


def _fee_and_tax_findings(
    proof: DecompositionProof, ledger: Ledger, contract: CompiledContract, ids: _IdSeq, audit_run_id: str
) -> list[Finding]:
    findings: list[Finding] = []
    for payment_ref in _resolved_payment_refs(proof):
        payment: Payment = ledger.get(payment_ref)
        breakdown = contract.fee_for(payment, at=payment.captured_at)
        currency = payment.amount.currency

        reported_fees = _reported_fee_totals(ledger, payment_ref)
        recomputed_fees = _recomputed_fee_totals(breakdown)
        for fee_type in sorted(set(reported_fees) | set(recomputed_fees), key=lambda t: t.value):
            delta = reported_fees.get(fee_type, 0) - recomputed_fees.get(fee_type, 0)
            if delta == 0:
                continue
            discrepancy_class = (
                DiscrepancyClass.FEE_UNDERCHARGE if delta < 0 else DiscrepancyClass.FEE_OVERCHARGE
            )
            findings.append(
                Finding(
                    id=ids.next(),
                    audit_run_id=audit_run_id,
                    discrepancy_class=discrepancy_class,
                    severity=Severity.MAJOR,
                    amount_impact=Money(abs(delta), currency),
                    evidence_ids=_evidence_for_fee_type(ledger, payment_ref, fee_type),
                    confidence=CONFIDENCE,
                    lane=Lane.PROPOSE,
                    explanation=(
                        f"payment {payment.id}: contract recomputes {fee_type.value} fee as "
                        f"{recomputed_fees.get(fee_type, 0)} paise but the settlement reports "
                        f"{reported_fees.get(fee_type, 0)} paise (delta {delta:+d} paise)"
                    ),
                )
            )

        reported_tax = _reported_tax_totals(ledger, payment_ref)
        recomputed_tax = _recomputed_tax_totals(breakdown)
        for fee_type in sorted(set(reported_tax) | set(recomputed_tax), key=lambda t: t.value):
            delta = reported_tax.get(fee_type, 0) - recomputed_tax.get(fee_type, 0)
            if delta == 0:
                continue
            findings.append(
                Finding(
                    id=ids.next(),
                    audit_run_id=audit_run_id,
                    discrepancy_class=DiscrepancyClass.TAX_MISCALCULATION,
                    severity=Severity.MAJOR,
                    amount_impact=Money(abs(delta), currency),
                    evidence_ids=_evidence_for_tax_of_fee_type(ledger, payment_ref, fee_type),
                    confidence=CONFIDENCE,
                    lane=Lane.PROPOSE,
                    explanation=(
                        f"payment {payment.id}: contract recomputes tax on {fee_type.value} as "
                        f"{recomputed_tax.get(fee_type, 0)} paise but the settlement reports "
                        f"{reported_tax.get(fee_type, 0)} paise (delta {delta:+d} paise)"
                    ),
                )
            )
    return findings


def _reversal_amount_pool(ledger: Ledger) -> Counter[int]:
    """Every MANUAL_CREDIT adjustment amount in the ledger, as a multiset a
    chargeback can consume one unit from. Built once per run (see
    verify_all) rather than once per proof: a WON chargeback's reversal is
    booked on its resolution day, routinely landing in a different
    proof/credit than the chargeback itself, so two proofs in the same run
    can legitimately compete for the same pool of reversals. Rebuilding a
    fresh, unconsumed pool for every proof -- the bug this replaces -- let
    two same-amount WON chargebacks in two different proofs each see the
    one real reversal as available and both pass, even though only one of
    them actually had it."""
    return Counter(
        ledger.get(ref).amount.paise
        for ref in ledger.by_type(EntityType.ADJUSTMENT)
        if ledger.get(ref).kind is AdjustmentKind.MANUAL_CREDIT
    )


def _chargeback_findings(
    proof: DecompositionProof,
    ledger: Ledger,
    ids: _IdSeq,
    audit_run_id: str,
    reversal_amounts: Counter[int],
) -> list[Finding]:
    chargeback_refs = sorted(
        {t.ref for t in proof.terms if t.ref.type is EntityType.CHARGEBACK}, key=lambda r: r.id
    )
    if not chargeback_refs:
        return []

    findings: list[Finding] = []
    for cb_ref in chargeback_refs:
        cb: Chargeback = ledger.get(cb_ref)
        if cb.stage is not ChargebackStage.WON:
            continue
        if reversal_amounts[cb.amount.paise] > 0:
            reversal_amounts[cb.amount.paise] -= 1
            continue
        findings.append(
            Finding(
                id=ids.next(),
                audit_run_id=audit_run_id,
                discrepancy_class=DiscrepancyClass.CHARGEBACK_AMOUNT_MISMATCH,
                severity=Severity.MAJOR,
                amount_impact=cb.amount,
                evidence_ids=[cb_ref],
                confidence=CONFIDENCE,
                lane=Lane.PROPOSE,
                explanation=(
                    f"chargeback {cb.id} is WON but no unmatched MANUAL_CREDIT adjustment of "
                    f"{cb.amount.to_rupees_str()} reverses it anywhere in the ledger"
                ),
            )
        )
    return findings


def verify(
    proof: DecompositionProof,
    ledger: Ledger,
    contract: CompiledContract,
    *,
    audit_run_id: str,
    reversal_amounts: Counter[int] | None = None,
) -> list[Finding]:
    """Independently recompute one proof's fee/tax lines and chargeback
    reversals; return every mismatch as a Finding.

    Skips AMBIGUOUS/UNRESOLVED proofs: terms is empty by construction, so
    there is nothing to recompute against, and their full credit amount
    already lands in conserve.py's unexplained_paise.

    `reversal_amounts`: the pool of ledger-wide MANUAL_CREDIT amounts a WON
    chargeback here can consume a match from. Defaults to a fresh pool built
    from the whole ledger, correct for a single proof taken in isolation.
    verify_all() instead builds one pool and threads it through every proof
    in a run, so two proofs cannot each see -- and both silently accept --
    the same single real reversal as satisfying their own chargeback.
    """
    if proof.outcome is not DecompositionOutcome.RESOLVED:
        return []
    if reversal_amounts is None:
        reversal_amounts = _reversal_amount_pool(ledger)
    ids = _IdSeq(audit_run_id, proof.credit_ref.id)
    return [
        *_fee_and_tax_findings(proof, ledger, contract, ids, audit_run_id),
        *_chargeback_findings(proof, ledger, ids, audit_run_id, reversal_amounts),
    ]


def verify_all(
    proofs: Iterable[DecompositionProof], ledger: Ledger, contract: CompiledContract, *, audit_run_id: str
) -> list[Finding]:
    reversal_amounts = _reversal_amount_pool(ledger)
    findings: list[Finding] = []
    for proof in sorted(proofs, key=lambda p: p.credit_ref.id):
        findings.extend(
            verify(proof, ledger, contract, audit_run_id=audit_run_id, reversal_amounts=reversal_amounts)
        )
    return findings
