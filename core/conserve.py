"""The conservation identity, asserted.

Only one piece of it is built so far: the sign convention. `decompose.py`
needs it before the full identity exists, because "which records produced
this credit" is unanswerable without knowing which way each record moves
money.

The convention is `datagen/world.py`'s, adopted deliberately rather than
re-derived. DECISIONS.md (2026-08-23) flagged two datagen-internal choices
for whoever built this module:

  - the adjustment sign tables (`RESERVE_HOLD`/`MANUAL_DEBIT` subtract;
    `RESERVE_RELEASE`/`MANUAL_CREDIT`/`FEE_WAIVER` add back), and
  - a won chargeback's reversal being a `MANUAL_CREDIT` `Adjustment`
    rather than a dedicated entity.

Both are adopted here. The second is why chargebacks subtract at *every*
stage including `WON`: the reversal is a separate record, so making a won
chargeback positive would credit it twice. `core/` may not import
`datagen/` (invariant 5), so this is a reimplementation, not a reuse --
`tests/test_conserve.py` reconciles the two against an independently
written formula.

Summing `signed_paise` over a batch's records reproduces the identity in
CLAUDE.md invariant 3:

    gross - refunds - fees - tax - chargebacks + add_back - subtract
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from core.ledger import Ledger
from core.models import AdjustmentKind, AssayModel, EntityType, RecordRef

if TYPE_CHECKING:
    from core.decompose import DecompositionProof

ADD_BACK_KINDS = frozenset(
    {
        AdjustmentKind.RESERVE_RELEASE,
        AdjustmentKind.MANUAL_CREDIT,
        AdjustmentKind.FEE_WAIVER,
    }
)

SUBTRACT_KINDS = frozenset(
    {
        AdjustmentKind.RESERVE_HOLD,
        AdjustmentKind.MANUAL_DEBIT,
    }
)

# Adjustments are absent: their direction is a property of the kind, not of
# the type. Every other contributing type has a fixed direction.
SIGN_BY_TYPE = {
    EntityType.PAYMENT: 1,
    EntityType.REFUND: -1,
    EntityType.CHARGEBACK: -1,
    EntityType.FEE_LINE: -1,
    EntityType.TAX_LINE: -1,
}

# FeeLine calls its money field `computed_amount`; TaxLine carries both
# `base_amount` and `amount` and only the tax itself is deducted. Naming the
# field per type beats a getattr(record, "amount") that would raise on one
# and quietly overstate the other.
MONEY_FIELD_BY_TYPE = {
    EntityType.PAYMENT: "amount",
    EntityType.REFUND: "amount",
    EntityType.CHARGEBACK: "amount",
    EntityType.ADJUSTMENT: "amount",
    EntityType.FEE_LINE: "computed_amount",
    EntityType.TAX_LINE: "amount",
}


def _sign_of(record: AssayModel) -> int:
    record_type = record.RECORD_TYPE
    if record_type is EntityType.ADJUSTMENT:
        if record.kind in ADD_BACK_KINDS:
            return 1
        if record.kind in SUBTRACT_KINDS:
            return -1
        raise ValueError(f"adjustment kind {record.kind!r} has no declared sign")
    sign = SIGN_BY_TYPE.get(record_type)
    if sign is None:
        raise ValueError(f"{record_type!r} makes no defined contribution to a settlement net")
    return sign


def money_of(record: AssayModel):
    """The single Money field that a record contributes, unsigned."""
    field = MONEY_FIELD_BY_TYPE.get(record.RECORD_TYPE)
    if field is None:
        raise ValueError(f"{record.RECORD_TYPE!r} makes no defined contribution to a settlement net")
    return getattr(record, field)


def signed_paise(record: AssayModel) -> int:
    """This record's signed contribution to a settlement net, in paise.

    Raises `ValueError` for types that make no contribution
    (`SETTLEMENT_BATCH`, `BANK_CREDIT`, `AUDIT_RUN`, `FINDING`,
    `JOURNAL_ENTRY`). Returning 0 for those would let a mis-typed record
    disappear from a sum with nothing to show for it -- and would let a
    bank credit be cited as a term in its own decomposition, which nets to
    zero and "balances" while explaining nothing.
    """
    return _sign_of(record) * money_of(record).paise


# ---------------------------------------------------------------------------
# Part two: bucketing one proof into the conservation identity.
#
# Every named bucket is stored as the POSITIVE MAGNITUDE it subtracts, so the
# identity reads exactly like CLAUDE.md invariant 3:
#
#     credit = settled_gross - refunds - fees - tax - chargebacks
#            - adjustments + reversals + unexplained
#
# `unexplained_paise` is signed (it IS proof.residual_paise, which can be
# negative when a credit's terms overexplain it) -- every other bucket is a
# magnitude, so a ConservationReport reads naturally on its own rather than
# needing a mental double-negative against invariant 3's prose.
#
# `unexplained_paise` is deliberately proof.residual_paise and nothing else:
# it is money that does not net against the settlement report's OWN claimed
# numbers. Whether those claimed numbers are themselves correct against the
# contract is a different question -- core/verify.py's, not this module's.
# A report can be perfectly self-consistent (unexplained_paise == 0) while
# every fee on it was computed off the wrong contract clause; conserve.py is
# structurally incapable of seeing that, on purpose. See core/verify.py's
# module docstring for the other half of this identity.
# ---------------------------------------------------------------------------


class ConservationViolation(Exception):
    """Raised when conserve()'s own bucket sums fail to reconstruct
    credit_paise exactly. This can only be a bug in this module --
    decompose.py already guarantees sum_paise + residual_paise ==
    credit_paise for every proof it emits -- so it stops a run rather than
    being scored, mirroring eval/determinism.py's NonDeterministicRun."""


class ConservationReport(AssayModel):
    credit_ref: RecordRef
    credit_paise: int
    currency: str

    settled_gross_paise: int  # PAYMENT terms
    refunds_paise: int  # REFUND terms, positive magnitude
    fees_paise: int  # FEE_LINE terms, positive magnitude
    tax_paise: int  # TAX_LINE terms, positive magnitude
    chargebacks_paise: int  # CHARGEBACK terms, positive magnitude
    adjustments_paise: int  # Adjustment terms in SUBTRACT_KINDS, positive magnitude
    reversals_paise: int  # Adjustment terms in ADD_BACK_KINDS, positive magnitude
    unexplained_paise: int  # == proof.residual_paise, signed


_BUCKET_TYPES = (
    EntityType.PAYMENT,
    EntityType.REFUND,
    EntityType.FEE_LINE,
    EntityType.TAX_LINE,
    EntityType.CHARGEBACK,
)


def conserve(proof: DecompositionProof, ledger: Ledger) -> ConservationReport:
    """Bucket one proof's terms into CLAUDE.md invariant 3's identity.

    No special-casing for AMBIGUOUS/UNRESOLVED: those proofs have empty
    terms, so every bucket is 0 and unexplained_paise carries the whole
    credit -- exactly invariant 3's fallback.
    """
    totals: dict[EntityType, int] = dict.fromkeys(_BUCKET_TYPES, 0)
    add_back = 0
    subtract = 0

    for term in proof.terms:
        ref = term.ref
        if ref.type is EntityType.ADJUSTMENT:
            record = ledger.get(ref)
            if record is None:
                raise ConservationViolation(
                    f"{proof.credit_ref.id}: adjustment {ref.id} is in the proof but not in the "
                    "ledger -- a bug in decompose.py or the caller, not this function"
                )
            if record.kind in ADD_BACK_KINDS:
                add_back += term.signed_paise
            elif record.kind in SUBTRACT_KINDS:
                subtract += term.signed_paise
            else:  # pragma: no cover -- test_conserve.py already proves this partition is total
                raise ConservationViolation(f"adjustment {ref.id} kind {record.kind!r} has no declared sign")
            continue
        if ref.type not in totals:
            raise ConservationViolation(
                f"{proof.credit_ref.id}: term {ref.id} is a {ref.type.value}, which makes no "
                "contribution to the identity -- decompose.py should never have cited it"
            )
        totals[ref.type] += term.signed_paise

    report = ConservationReport(
        credit_ref=proof.credit_ref,
        credit_paise=proof.credit_paise,
        currency=proof.currency,
        settled_gross_paise=totals[EntityType.PAYMENT],
        refunds_paise=-totals[EntityType.REFUND],
        fees_paise=-totals[EntityType.FEE_LINE],
        tax_paise=-totals[EntityType.TAX_LINE],
        chargebacks_paise=-totals[EntityType.CHARGEBACK],
        adjustments_paise=-subtract,
        reversals_paise=add_back,
        unexplained_paise=proof.residual_paise,
    )

    reconstructed = (
        report.settled_gross_paise
        - report.refunds_paise
        - report.fees_paise
        - report.tax_paise
        - report.chargebacks_paise
        - report.adjustments_paise
        + report.reversals_paise
        + report.unexplained_paise
    )
    if reconstructed != proof.credit_paise:
        raise ConservationViolation(
            f"{proof.credit_ref.id}: buckets reconstruct to {reconstructed} paise, not "
            f"credit_paise {proof.credit_paise} -- a bug in conserve(), not an audit finding"
        )
    return report


def conserve_all(proofs: Iterable[DecompositionProof], ledger: Ledger) -> list[ConservationReport]:
    return [conserve(proof, ledger) for proof in proofs]


def total_unexplained_paise(reports: Iterable[ConservationReport]) -> int:
    """Named rather than left as sum(...) at each call site: this is the
    number invariant 3 calls a first-class, reportable bucket, and every
    caller (tests, a run summary) needs the same one.

    This is NOT the whole of invariant 3's promise by itself: it only sums
    the residual on credits that exist. A settlement batch whose bank
    credit never arrived at all -- genuinely never paid out -- contributes
    no proof, so no ConservationReport, so nothing here. See
    unclaimed_paise() for the other half.
    """
    return sum(r.unexplained_paise for r in reports)


def unclaimed_records(ledger: Ledger, proofs: Iterable[DecompositionProof]) -> list[RecordRef]:
    """Every money-contributing record in the ledger that no proof in this
    run claimed as a term.

    decompose_all only ever looks at the credits it is handed -- the bank
    statement's list, not the ledger's. A settlement batch that was formed
    and reported but whose bank credit never actually arrived (a real short
    settlement) has every one of its payment/fee/tax records sitting here,
    and total_unexplained_paise alone will never see it: with no credit,
    there is no proof, so no residual is ever computed for it, and a run
    that lost an entire batch reports as perfectly clean. This closes that
    gap by checking the ledger itself, not just the credits that showed up.
    """
    claimed = {term.ref for proof in proofs for term in proof.terms}
    unclaimed: list[RecordRef] = []
    for ref in ledger:
        record = ledger.get(ref)
        try:
            signed_paise(record)
        except ValueError:
            continue  # non-contributing type: SETTLEMENT_BATCH, BANK_CREDIT, AUDIT_RUN, FINDING, JOURNAL_ENTRY
        if ref not in claimed:
            unclaimed.append(ref)
    return sorted(unclaimed, key=lambda r: (r.type.value, r.id))


def unclaimed_paise(ledger: Ledger, proofs: Iterable[DecompositionProof]) -> int:
    """Net signed paise sitting in every unclaimed record: money the run
    never even attempted to explain, as distinct from total_unexplained_paise
    (money a credit tried to explain and could not). Add the two for the
    full amount invariant 3 requires an audit to account for."""
    proofs = list(proofs)
    return sum(signed_paise(ledger.get(ref)) for ref in unclaimed_records(ledger, proofs))
