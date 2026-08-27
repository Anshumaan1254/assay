"""Bank credit -> transaction subset, with proof.

Given one BankCredit, find the transactions that produced it and emit a
proof another program can re-derive from the ledger alone.

Three tiers, first success wins:

  1. STRUCTURAL -- exact join on the UTR. Resolves the large majority.
  2. SUBSET_SUM -- bounded dynamic programming over integer paise, for
     credits whose settlement id is missing or whose UTR is corrupt.
  3. ASSIGNMENT -- min-cost 1:1 pairing (Hungarian) over a feature-based
     cost, for the residue where no subset sums exactly.

Two properties matter more than resolution rate:

**It refuses to choose.** When more than one transaction set explains a
credit, the result is AMBIGUOUS with the competitors named, and `terms` is
empty. That is true of subset-sum finding two subsets, and of the
assignment's runner-up costing the same as the winner. Silently picking one
is the failure mode this module is built around; it would produce a clean
report over the wrong records, with nothing anywhere to indicate it.

**A structural hit never cascades.** If the UTR names a batch whose records
do not net to the credit, that is still the right record set -- the
difference is the finding. Falling through to tier 2 would let the search
discover some *other* subset that happens to net exactly, and the shortfall
would vanish from the report. So tier 1 resolves with a non-zero
`residual_paise`, which is what feeds invariant 3's `unexplained` bucket.

Budgets: the node budget is the binding bound and is reproducible across
machines. The wall clock is a safety net so a pathological input degrades
to an exception rather than hanging; a run that trips it is not covered by
invariant 4's byte-identical guarantee, which is why the two bail out with
different reasons rather than one shared "budget exhausted".
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import ClassVar

import numpy as np
from dotenv import load_dotenv
from pydantic import Field
from scipy.optimize import linear_sum_assignment

from core.conserve import money_of, signed_paise
from core.contract import canonical_json, sha256_of
from core.lanes import CalibrationArtifact, LaneAssignment, assign_lane_for_proof
from core.ledger import Ledger
from core.models import AssayModel, BankCredit, EntityType, RecordRef

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class DecompositionTier(StrEnum):
    STRUCTURAL = "structural"
    SUBSET_SUM = "subset_sum"
    ASSIGNMENT = "assignment"


class DecompositionOutcome(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class DecompositionReason(StrEnum):
    """Fixed vocabulary. Every non-resolution names one of these -- free-text
    reasons cannot be counted in `eval/` or clustered in the exception
    taxonomy."""

    NO_STRUCTURAL_KEY = "no_structural_key"
    STRUCTURAL_KEY_NOT_FOUND = "structural_key_not_found"
    STRUCTURAL_KEY_AMBIGUOUS = "structural_key_ambiguous"
    CREDIT_SHARES_STRUCTURAL_KEY = "credit_shares_structural_key"
    STRUCTURAL_RECORDS_ALREADY_CLAIMED = "structural_records_already_claimed"
    NO_CANDIDATES_IN_WINDOW = "no_candidates_in_window"
    TOO_MANY_CANDIDATES = "too_many_candidates"
    NO_SUBSET_SUMS_TO_CREDIT = "no_subset_sums_to_credit"
    MULTIPLE_SUBSETS_MATCH = "multiple_subsets_match"
    NODE_BUDGET_EXHAUSTED = "node_budget_exhausted"
    TIME_BUDGET_EXHAUSTED = "time_budget_exhausted"
    ASSIGNMENT_TIE = "assignment_tie"
    NO_ASSIGNMENT_FEASIBLE = "no_assignment_feasible"
    CURRENCY_MISMATCH = "currency_mismatch"


# Confidence is basis points, matching Finding.confidence. These are fixed
# per tier on purpose: calibrating a real score is core/lanes.py's job
# (conformal-calibrated autonomy lanes), and inventing a formula here would
# put an uncalibrated number in front of a reviewer. The raw features the
# calibration will need travel on the proof instead.
CONFIDENCE_BY_TIER = {
    DecompositionTier.STRUCTURAL: 10_000,
    DecompositionTier.SUBSET_SUM: 8_000,
    DecompositionTier.ASSIGNMENT: 6_000,
}

# Tier 3 cost weights. All integer -- core/ contains no float literal
# (tests/test_architecture.py) and a float cost would make ties fuzzy, which
# is exactly the thing tier 3 must detect exactly.
_W_DATE = 1_000  # per day of distance from the expected credit date
_W_AMOUNT = 1  # per unit of scaled amount distance
_W_NARRATION = 5_000  # full weight when narration and unit share no tokens
_AMOUNT_SCALE = 100  # paise per unit of amount cost, i.e. one rupee
_AMOUNT_COST_CAP = 1_000_000  # beyond this, "far" is far; keeps costs < 2**53
_BPS = 10_000

# Forbidden / impossible edge. Large enough to never win, small enough that
# a whole assignment of them stays exact in the float64 scipy converts to.
_UNASSIGNED_COST = 1 << 40

_CLOCK_CHECK_INTERVAL = 1_024

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# T+2 is the standard settlement cycle in India, but it is a contracted
# arrangement rather than a law of arithmetic -- a merchant on T+1 or T+3 is
# ordinary. Declared here as the default and overridable through
# SETTLEMENT_LAG_DAYS in the environment, so changing it never means editing
# code. It shapes tier 3's date cost and nothing else; no amount anywhere
# depends on it.
SETTLEMENT_LAG_DAYS = 2

_CONTRIBUTING_TYPES = (
    EntityType.PAYMENT,
    EntityType.REFUND,
    EntityType.CHARGEBACK,
    EntityType.ADJUSTMENT,
)

# A budget bail-out is terminal: escalating to a more expensive tier after
# running out of budget on a cheaper one is not a search strategy.
_TERMINAL_REASONS = frozenset(
    {
        DecompositionReason.NODE_BUDGET_EXHAUSTED,
        DecompositionReason.TIME_BUDGET_EXHAUSTED,
        DecompositionReason.CURRENCY_MISMATCH,
    }
)


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class ProofTerm(AssayModel):
    ref: RecordRef
    signed_paise: int


class CandidateSet(AssayModel):
    """One of the transaction sets that explained a credit equally well.
    Populated only when the outcome is AMBIGUOUS."""

    refs: list[RecordRef]
    sum_paise: int


class DecompositionBudget(AssayModel):
    max_nodes: int = 200_000
    max_candidates: int = 40
    time_budget_ns: int = 2_000_000_000
    window_days_before: int = 5
    window_days_after: int = 5
    max_competing: int = 4
    # A matching hint only -- how long after a cycle closes its credit
    # normally lands. See SETTLEMENT_LAG_DAYS above.
    settlement_lag_days: int = SETTLEMENT_LAG_DAYS

    @classmethod
    def from_env(cls) -> DecompositionBudget:
        """Budget with any environment overrides applied.

        Deliberately explicit rather than read inside `__init__`: a budget
        that silently absorbed ambient environment would make every run
        depend on the shell it was launched from, and invariant 4's
        byte-identical guarantee could not be checked. Callers that want
        configuration ask for it; `DecompositionBudget()` stays pure.

        Only the settlement lag is wired up, because it is the only value
        here that describes the merchant's arrangement rather than this
        machine's patience. The other fields are search budgets; extend this
        the same way if one of them ever needs to vary by deployment.
        """
        load_dotenv()
        return cls(
            settlement_lag_days=int(
                os.environ.get("SETTLEMENT_LAG_DAYS", cls.model_fields["settlement_lag_days"].default)
            )
        )


DEFAULT_BUDGET = DecompositionBudget()


class DecompositionProof(AssayModel):
    """Why this credit is these transactions.

    Independently checkable: `verify_proof(proof, ledger)` re-derives every
    term's amount from the ledger and re-sums them, needing nothing else.
    """

    # Observed, not derived. Excluded from proof_hash so that two runs over
    # identical inputs still produce identical hashes (invariant 4).
    # lane_assignment joins elapsed_ns here for the same reason: it depends
    # on an external, refittable CalibrationArtifact rather than on "why
    # this credit is these transactions" -- the proof's own job -- so which
    # artifact (or none) was supplied at decompose time must never change
    # proof_hash.
    TELEMETRY_FIELDS: ClassVar[frozenset[str]] = frozenset({"elapsed_ns", "lane_assignment"})

    credit_ref: RecordRef
    credit_paise: int
    currency: str

    tier: DecompositionTier
    tiers_attempted: list[DecompositionTier]
    declined_reasons: list[DecompositionReason]
    outcome: DecompositionOutcome
    reason: DecompositionReason | None

    terms: list[ProofTerm]
    sum_paise: int
    residual_paise: int
    competing: list[CandidateSet]

    confidence: int = Field(ge=0, le=10_000)

    # Raw features for core/lanes.py to calibrate against later.
    candidate_count: int
    subset_size: int
    nodes_expanded: int
    assignment_cost: int | None
    assignment_margin: int | None

    elapsed_ns: int
    proof_hash: str

    # None when decompose()/decompose_all() were called without a
    # calibration argument -- the common, backward-compatible case. Set by
    # core/lanes.py's assign_lane_for_proof() when one is supplied.
    lane_assignment: LaneAssignment | None = None


class ProofVerification(AssayModel):
    ok: bool
    failures: list[str]


# --------------------------------------------------------------------------
# Settlement units
#
# The atom of a decomposition is a unit, not a record. Two reasons:
# semantically you cannot include a payment's fee without the payment; and
# at record granularity a credit in the committed run faces ~168 payments
# and 2**168 subsets, so the node budget would trip on every credit, every
# time. Grouping first is what makes tier 2 tractable at all.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Unit:
    key: str
    refs: tuple[RecordRef, ...]
    net_paise: int
    day: date
    merchant_id: str
    currency: str
    tokens: frozenset[str]


def _tokens(*texts: str) -> frozenset[str]:
    found: set[str] = set()
    for text in texts:
        found.update(_TOKEN_RE.findall(text.lower()))
    return frozenset(found)


def _record_day(record: AssayModel) -> date:
    """The record's own primary timestamp, which is what datagen keys a
    settlement cycle off."""
    for field in ("captured_at", "created_at", "raised_at", "cycle_end"):
        stamp = getattr(record, field, None)
        if stamp is not None:
            return stamp.date()
    raise ValueError(f"{record.RECORD_TYPE!r} has no primary timestamp")


def _with_fee_and_tax_lines(ledger: Ledger, ref: RecordRef) -> list[RecordRef]:
    """`ref` plus every fee line charged against it and every tax line on
    those fees. FeeLine and TaxLine carry no settlement_id, so this
    transitive walk is the only way to reach them."""
    refs = [ref]
    for fee_ref in ledger.fee_lines_for(ref):
        refs.append(fee_ref)
        refs.extend(ledger.tax_lines_for(fee_ref))
    return refs


def _batch_member_refs(ledger: Ledger, batch_id: str) -> list[RecordRef]:
    refs: list[RecordRef] = []
    for ref in ledger.by_settlement(batch_id):
        refs.extend(_with_fee_and_tax_lines(ledger, ref))
    return sorted(set(refs), key=lambda r: (r.type.value, r.id))


def _make_unit(ledger: Ledger, key: str, refs: Sequence[RecordRef], merchant_id: str, day: date) -> _Unit | None:
    """None when the refs contribute nothing (an empty batch)."""
    if not refs:
        return None
    net = 0
    currencies: set[str] = set()
    for ref in refs:
        record = ledger.get(ref)
        if record is None:
            return None
        net += signed_paise(record)
        currencies.add(money_of(record).currency)
    if len(currencies) != 1:
        # Mixed currencies inside one settlement unit is a data error, not a
        # matching problem. Refusing the unit keeps it out of every search.
        return None
    return _Unit(
        key=key,
        refs=tuple(refs),
        net_paise=net,
        day=day,
        merchant_id=merchant_id,
        currency=currencies.pop(),
        tokens=_tokens(key, *(ref.id for ref in refs)),
    )


def _batch_unit(ledger: Ledger, batch_ref: RecordRef) -> _Unit | None:
    batch = ledger.get(batch_ref)
    if batch is None:
        return None
    refs = _batch_member_refs(ledger, batch.id)
    unit = _make_unit(ledger, batch.id, refs, batch.merchant_id, _record_day(batch))
    if unit is None:
        return None
    return _Unit(
        key=unit.key,
        refs=unit.refs,
        net_paise=unit.net_paise,
        day=unit.day,
        merchant_id=unit.merchant_id,
        currency=unit.currency,
        tokens=unit.tokens | _tokens(batch.utr),
    )


def _sub_units_of_batch(ledger: Ledger, batch_ref: RecordRef) -> list[_Unit]:
    """A batch broken into its constituent movements -- each payment with its
    own fees and taxes, and each non-payment record on its own.

    Used only for a batch whose UTR is claimed by more than one credit: a
    split settlement. The Hungarian method in tier 3 is 1:1 and structurally
    cannot express "this credit is payments 0 and 1", so a split is resolved
    by subset-sum over the sub-units instead. Whole-batch and sub-unit
    granularity are never offered together -- the whole would always equal
    the sum of its parts, and every such credit would report as ambiguous.
    """
    batch = ledger.get(batch_ref)
    if batch is None:
        return []
    units: list[_Unit] = []
    for member in ledger.by_settlement(batch.id):
        refs = _with_fee_and_tax_lines(ledger, member) if member.type is EntityType.PAYMENT else [member]
        record = ledger.get(member)
        if record is None:
            continue
        unit = _make_unit(ledger, member.id, refs, batch.merchant_id, _record_day(record))
        if unit is not None:
            units.append(unit)
    return units


def _orphan_units(ledger: Ledger, merchant_id: str) -> list[_Unit]:
    """Records that belong to no settlement -- datagen's D08 shape, a
    captured payment that never reached a batch."""
    units: list[_Unit] = []
    for entity_type in _CONTRIBUTING_TYPES:
        for ref in ledger.by_type(entity_type):
            record = ledger.get(ref)
            if record is None or getattr(record, "settlement_id", None) is not None:
                continue
            refs = _with_fee_and_tax_lines(ledger, ref) if entity_type is EntityType.PAYMENT else [ref]
            owner = getattr(record, "merchant_id", merchant_id)
            unit = _make_unit(ledger, ref.id, refs, owner, _record_day(record))
            if unit is not None:
                units.append(unit)
    return units


# --------------------------------------------------------------------------
# Tier 1 -- structural
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _TierResult:
    unit: _Unit | None = None
    reason: DecompositionReason | None = None
    competing: tuple[_Unit, ...] = ()
    candidate_count: int = 0
    nodes: int = 0
    assignment_cost: int | None = None
    assignment_margin: int | None = None


def _tier_structural(
    credit: BankCredit,
    ledger: Ledger,
    claimed: frozenset[RecordRef],
    shared_utrs: frozenset[str],
) -> _TierResult:
    if not credit.utr:
        return _TierResult(reason=DecompositionReason.NO_STRUCTURAL_KEY)
    if credit.utr in shared_utrs:
        # More than one credit names this UTR: a split settlement. Resolving
        # both to the whole batch would double-claim every record in it.
        return _TierResult(reason=DecompositionReason.CREDIT_SHARES_STRUCTURAL_KEY)

    batch_refs = [ref for ref in ledger.by_utr(credit.utr) if ref.type is EntityType.SETTLEMENT_BATCH]
    if not batch_refs:
        # Exact equality only. A truncated UTR (datagen's D09) is corrupt
        # data and must be seen to fall through -- repairing it with a
        # prefix match inside the most-trusted tier would hide the
        # data-quality problem behind a confident answer.
        return _TierResult(reason=DecompositionReason.STRUCTURAL_KEY_NOT_FOUND)
    if len(batch_refs) > 1:
        return _TierResult(reason=DecompositionReason.STRUCTURAL_KEY_AMBIGUOUS, candidate_count=len(batch_refs))

    unit = _batch_unit(ledger, batch_refs[0])
    if unit is None:
        return _TierResult(reason=DecompositionReason.STRUCTURAL_KEY_NOT_FOUND)
    if unit.currency != credit.amount.currency:
        return _TierResult(reason=DecompositionReason.CURRENCY_MISMATCH, candidate_count=1)
    if set(unit.refs) & claimed:
        return _TierResult(reason=DecompositionReason.STRUCTURAL_RECORDS_ALREADY_CLAIMED, candidate_count=1)
    return _TierResult(unit=unit, candidate_count=1)


# --------------------------------------------------------------------------
# Tier 2 -- constrained subset reconstruction
# --------------------------------------------------------------------------


def _candidate_units(
    credit: BankCredit,
    ledger: Ledger,
    merchant_id: str,
    claimed: frozenset[RecordRef],
    split_batch_ids: frozenset[str],
    budget: DecompositionBudget,
) -> tuple[list[_Unit], int]:
    """Units plausibly in play for this credit, plus how many were dropped
    solely for currency."""
    pool: list[_Unit] = []
    for batch_ref in ledger.by_type(EntityType.SETTLEMENT_BATCH):
        if batch_ref.id in split_batch_ids:
            pool.extend(_sub_units_of_batch(ledger, batch_ref))
        else:
            unit = _batch_unit(ledger, batch_ref)
            if unit is not None:
                pool.append(unit)
    pool.extend(_orphan_units(ledger, merchant_id))

    earliest = credit.value_date - timedelta(days=budget.window_days_before)
    latest = credit.value_date + timedelta(days=budget.window_days_after)

    kept: list[_Unit] = []
    currency_rejects = 0
    for unit in pool:
        if unit.merchant_id != merchant_id:
            continue
        # Half-open on the upper bound, matching the [min, max) convention
        # used for amount bands and effective dating elsewhere in core/.
        if not (earliest <= unit.day < latest):
            continue
        if set(unit.refs) & claimed:
            continue
        if unit.currency != credit.amount.currency:
            currency_rejects += 1
            continue
        kept.append(unit)

    kept.sort(key=lambda u: (u.key, u.net_paise))
    return kept, currency_rejects


@dataclass(frozen=True, slots=True)
class _SearchOutcome:
    witnesses: tuple[tuple[int, ...], ...]
    nodes: int
    reason: DecompositionReason | None = None


def _subset_search(
    units: Sequence[_Unit],
    target: int,
    budget: DecompositionBudget,
    clock: Callable[[], int],
    start_ns: int,
) -> _SearchOutcome:
    """Every subset of `units` netting exactly to `target`, up to
    `max_competing` of them.

    A dict of reachable partial sums rather than the textbook array: unit
    nets are signed (a standalone refund is negative), and a positive-only
    DP silently cannot reach a target that requires subtracting one.

    The pruning is what keeps it bounded. `suffix_max`/`suffix_min` are the
    most a partial sum can still gain or lose from the units not yet seen,
    so any partial sum that can no longer reach the target is dropped
    immediately rather than carried to the end.
    """
    n = len(units)
    suffix_max = [0] * (n + 1)
    suffix_min = [0] * (n + 1)
    for i in range(n - 1, -1, -1):
        net = units[i].net_paise
        suffix_max[i] = suffix_max[i + 1] + max(net, 0)
        suffix_min[i] = suffix_min[i + 1] + min(net, 0)

    states: dict[int, list[tuple[int, ...]]] = {0: [()]}
    nodes = 0

    for i, unit in enumerate(units):
        if clock() - start_ns > budget.time_budget_ns:
            return _SearchOutcome((), nodes, DecompositionReason.TIME_BUDGET_EXHAUSTED)

        low, high = suffix_min[i + 1], suffix_max[i + 1]
        nxt: dict[int, list[tuple[int, ...]]] = {}
        for partial, witnesses in states.items():
            for reached, suffix in ((partial, ()), (partial + unit.net_paise, (i,))):
                if not (reached + low <= target <= reached + high):
                    continue
                bucket = nxt.setdefault(reached, [])
                for witness in witnesses:
                    if len(bucket) >= budget.max_competing:
                        break
                    bucket.append(witness + suffix)
                    nodes += 1
                    if nodes > budget.max_nodes:
                        return _SearchOutcome((), nodes, DecompositionReason.NODE_BUDGET_EXHAUSTED)
                    if nodes % _CLOCK_CHECK_INTERVAL == 0 and clock() - start_ns > budget.time_budget_ns:
                        return _SearchOutcome((), nodes, DecompositionReason.TIME_BUDGET_EXHAUSTED)
        states = nxt

    # The empty subset explains nothing, even when the credit is zero.
    found = tuple(w for w in states.get(target, []) if w)
    return _SearchOutcome(found, nodes)


def _tier_subset_sum(
    credit: BankCredit,
    ledger: Ledger,
    merchant_id: str,
    claimed: frozenset[RecordRef],
    split_batch_ids: frozenset[str],
    budget: DecompositionBudget,
    clock: Callable[[], int],
    start_ns: int,
) -> tuple[_TierResult, list[_Unit]]:
    units, currency_rejects = _candidate_units(credit, ledger, merchant_id, claimed, split_batch_ids, budget)
    if not units:
        reason = (
            DecompositionReason.CURRENCY_MISMATCH
            if currency_rejects
            else DecompositionReason.NO_CANDIDATES_IN_WINDOW
        )
        return _TierResult(reason=reason), []
    if len(units) > budget.max_candidates:
        return _TierResult(reason=DecompositionReason.TOO_MANY_CANDIDATES, candidate_count=len(units)), units

    outcome = _subset_search(units, credit.amount.paise, budget, clock, start_ns)
    if outcome.reason is not None:
        return _TierResult(reason=outcome.reason, candidate_count=len(units), nodes=outcome.nodes), units
    if not outcome.witnesses:
        return (
            _TierResult(
                reason=DecompositionReason.NO_SUBSET_SUMS_TO_CREDIT,
                candidate_count=len(units),
                nodes=outcome.nodes,
            ),
            units,
        )
    if len(outcome.witnesses) > 1:
        competing = tuple(_merge_units([units[i] for i in w]) for w in outcome.witnesses)
        return (
            _TierResult(
                reason=DecompositionReason.MULTIPLE_SUBSETS_MATCH,
                competing=competing,
                candidate_count=len(units),
                nodes=outcome.nodes,
            ),
            units,
        )

    chosen = _merge_units([units[i] for i in outcome.witnesses[0]])
    return _TierResult(unit=chosen, candidate_count=len(units), nodes=outcome.nodes), units


def _merge_units(units: Sequence[_Unit]) -> _Unit:
    refs: list[RecordRef] = []
    tokens: set[str] = set()
    net = 0
    for unit in units:
        refs.extend(unit.refs)
        tokens.update(unit.tokens)
        net += unit.net_paise
    first = units[0]
    return _Unit(
        key="+".join(u.key for u in units),
        refs=tuple(sorted(set(refs), key=lambda r: (r.type.value, r.id))),
        net_paise=net,
        day=min(u.day for u in units),
        merchant_id=first.merchant_id,
        currency=first.currency,
        tokens=frozenset(tokens),
    )


# --------------------------------------------------------------------------
# Tier 3 -- cost-based assignment
# --------------------------------------------------------------------------


def _jaccard_bps(left: frozenset[str], right: frozenset[str]) -> int:
    union = left | right
    if not union:
        return 0
    return _BPS * len(left & right) // len(union)


def _pair_cost(credit: BankCredit, unit: _Unit, budget: DecompositionBudget) -> int:
    """Integer cost of explaining `credit` with `unit`. Lower is better.

    The date term measures distance from the *expected* credit date, not
    from the cycle close: a settlement lands settlement_lag_days later by
    design, so comparing against the raw cycle date would systematically
    favour the wrong batch.
    """
    expected = unit.day + timedelta(days=budget.settlement_lag_days)
    date_cost = abs((expected - credit.value_date).days) * _W_DATE

    gap = abs(unit.net_paise - credit.amount.paise)
    amount_cost = min(gap // _AMOUNT_SCALE, _AMOUNT_COST_CAP) * _W_AMOUNT

    narration_cost = (_BPS - _jaccard_bps(_tokens(credit.narration), unit.tokens)) * _W_NARRATION // _BPS

    return date_cost + amount_cost + narration_cost


def _solve(matrix: np.ndarray) -> tuple[list[tuple[int, int]], int]:
    rows, cols = linear_sum_assignment(matrix)
    pairs = [(int(r), int(c)) for r, c in zip(rows, cols)]
    total = sum(int(matrix[r][c]) for r, c in pairs)
    return pairs, total


def _tier_assignment(
    credits: Sequence[BankCredit],
    units: Sequence[_Unit],
    budget: DecompositionBudget,
) -> dict[str, _TierResult]:
    """Best 1:1 pairing of unresolved credits to unclaimed units, with the
    margin over the runner-up.

    The margin does double duty. It is the confidence signal the spec asks
    for -- a low margin means the second-best pairing was nearly as good, so
    the result routes to ESCALATE. And a margin of exactly zero means the
    solver's pick was arbitrary between two equal optima, which is both a
    silent choice and the one place scipy's undocumented tie-breaking could
    make a report differ across BLAS builds. A tie is reported as AMBIGUOUS
    for every credit in the assignment rather than resolved for any of them:
    conservative on purpose, since a tie anywhere means the whole pairing
    could have come out differently.
    """
    results: dict[str, _TierResult] = {}
    if not units:
        return {c.id: _TierResult(reason=DecompositionReason.NO_ASSIGNMENT_FEASIBLE) for c in credits}

    matrix = np.array(
        [[_pair_cost(credit, unit, budget) for unit in units] for credit in credits],
        dtype=np.int64,
    )
    pairs, best_cost = _solve(matrix)

    runner_up_cost = _UNASSIGNED_COST * max(len(credits), 1)
    runner_up_pairs: list[tuple[int, int]] = []
    for row, col in pairs:
        forbidden = matrix.copy()
        forbidden[row][col] = _UNASSIGNED_COST
        alternative, cost = _solve(forbidden)
        if cost < runner_up_cost:
            runner_up_cost = cost
            runner_up_pairs = alternative

    margin = runner_up_cost - best_cost
    assigned = dict(pairs)
    runner_up = dict(runner_up_pairs)

    for row, credit in enumerate(credits):
        if row not in assigned:
            results[credit.id] = _TierResult(
                reason=DecompositionReason.NO_ASSIGNMENT_FEASIBLE,
                candidate_count=len(units),
            )
            continue
        unit = units[assigned[row]]
        if margin == 0:
            competing = [unit]
            other = runner_up.get(row)
            if other is not None and other != assigned[row]:
                competing.append(units[other])
            results[credit.id] = _TierResult(
                reason=DecompositionReason.ASSIGNMENT_TIE,
                competing=tuple(competing),
                candidate_count=len(units),
                assignment_cost=best_cost,
                assignment_margin=margin,
            )
            continue
        results[credit.id] = _TierResult(
            unit=unit,
            candidate_count=len(units),
            assignment_cost=best_cost,
            assignment_margin=margin,
        )
    return results


# --------------------------------------------------------------------------
# Proof assembly
# --------------------------------------------------------------------------


def _terms_of(ledger: Ledger, unit: _Unit) -> list[ProofTerm]:
    return [
        ProofTerm(ref=ref, signed_paise=signed_paise(ledger.get(ref)))
        for ref in sorted(unit.refs, key=lambda r: (r.type.value, r.id))
    ]


def _hash_payload(proof: DecompositionProof) -> str:
    payload = proof.model_dump(mode="json")
    for field in DecompositionProof.TELEMETRY_FIELDS:
        payload.pop(field, None)
    payload.pop("proof_hash", None)
    return sha256_of(canonical_json(payload))


def _build_proof(
    credit: BankCredit,
    ledger: Ledger,
    *,
    tier: DecompositionTier,
    tiers_attempted: Sequence[DecompositionTier],
    declined: Sequence[DecompositionReason],
    result: _TierResult,
    elapsed_ns: int,
    calibration: CalibrationArtifact | None = None,
) -> DecompositionProof:
    if result.unit is not None:
        outcome = DecompositionOutcome.RESOLVED
        terms = _terms_of(ledger, result.unit)
        subset_size = len(result.unit.key.split("+"))
        confidence = CONFIDENCE_BY_TIER[tier]
        competing: list[CandidateSet] = []
    elif result.competing:
        outcome = DecompositionOutcome.AMBIGUOUS
        terms = []
        subset_size = 0
        confidence = 0
        competing = [
            CandidateSet(refs=list(unit.refs), sum_paise=unit.net_paise) for unit in result.competing
        ]
    else:
        outcome = DecompositionOutcome.UNRESOLVED
        terms = []
        subset_size = 0
        confidence = 0
        competing = []

    total = sum(term.signed_paise for term in terms)
    proof = DecompositionProof(
        credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id=credit.id),
        credit_paise=credit.amount.paise,
        currency=credit.amount.currency,
        tier=tier,
        tiers_attempted=list(tiers_attempted),
        declined_reasons=list(declined),
        outcome=outcome,
        reason=result.reason,
        terms=terms,
        sum_paise=total,
        residual_paise=credit.amount.paise - total,
        competing=competing,
        confidence=confidence,
        candidate_count=result.candidate_count,
        subset_size=subset_size,
        nodes_expanded=result.nodes,
        assignment_cost=result.assignment_cost,
        assignment_margin=result.assignment_margin,
        elapsed_ns=elapsed_ns,
        proof_hash="",
    )
    if calibration is not None:
        proof = proof.model_copy(update={"lane_assignment": assign_lane_for_proof(proof, calibration)})
    return proof.model_copy(update={"proof_hash": _hash_payload(proof)})


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def decompose(
    credit: BankCredit,
    ledger: Ledger,
    *,
    merchant_id: str,
    budget: DecompositionBudget = DEFAULT_BUDGET,
    claimed: frozenset[RecordRef] = frozenset(),
    split_batch_ids: frozenset[str] = frozenset(),
    shared_utrs: frozenset[str] = frozenset(),
    clock: Callable[[], int] = time.monotonic_ns,
    calibration: CalibrationArtifact | None = None,
) -> DecompositionProof:
    """Decompose one credit through tiers 1 and 2.

    Tier 3 is a property of a whole batch of credits -- it pairs the
    unresolved ones against the unclaimed units -- so it lives in
    `decompose_all`.

    `merchant_id` is an explicit argument because BankCredit has no
    merchant_id field; the audit scope supplies it rather than this module
    guessing one out of the narration.

    `calibration`, when supplied, stamps the resulting proof's
    `lane_assignment` via core/lanes.py's assign_lane_for_proof(). Defaults
    to None -- decompose() stays fully usable without ever loading a
    calibration artifact, which is what every existing caller does today.
    """
    start_ns = clock()
    tiers: list[DecompositionTier] = [DecompositionTier.STRUCTURAL]
    declined: list[DecompositionReason] = []

    structural = _tier_structural(credit, ledger, claimed, shared_utrs)
    if structural.unit is not None:
        return _build_proof(
            credit,
            ledger,
            tier=DecompositionTier.STRUCTURAL,
            tiers_attempted=tiers,
            declined=declined,
            result=structural,
            elapsed_ns=clock() - start_ns,
            calibration=calibration,
        )
    if structural.reason in _TERMINAL_REASONS:
        return _build_proof(
            credit,
            ledger,
            tier=DecompositionTier.STRUCTURAL,
            tiers_attempted=tiers,
            declined=declined,
            result=structural,
            elapsed_ns=clock() - start_ns,
            calibration=calibration,
        )
    if structural.reason is not None:
        declined.append(structural.reason)

    tiers.append(DecompositionTier.SUBSET_SUM)
    subset, _units = _tier_subset_sum(
        credit, ledger, merchant_id, claimed, split_batch_ids, budget, clock, start_ns
    )
    return _build_proof(
        credit,
        ledger,
        tier=DecompositionTier.SUBSET_SUM,
        tiers_attempted=tiers,
        declined=declined,
        result=subset,
        elapsed_ns=clock() - start_ns,
        calibration=calibration,
    )


def _may_escalate(proof: DecompositionProof) -> bool:
    if proof.outcome is not DecompositionOutcome.UNRESOLVED:
        return False
    return proof.reason not in _TERMINAL_REASONS


def decompose_all(
    credits: Iterable[BankCredit],
    ledger: Ledger,
    *,
    merchant_id: str,
    budget: DecompositionBudget = DEFAULT_BUDGET,
    clock: Callable[[], int] = time.monotonic_ns,
    calibration: CalibrationArtifact | None = None,
) -> list[DecompositionProof]:
    """Decompose a whole bank statement.

    Two full phases across every credit, not two tiers cascaded per credit.
    A structural sweep over EVERY credit runs to completion first, claiming
    every record it can account for with certainty; only then does
    subset-sum run, over the residue, for whatever didn't resolve
    structurally. Without that separation, a credit that falls to tier 2
    early in id order could speculatively claim records that structurally
    belong to a credit processed later -- pre-empting that later credit's
    own certain UTR join with an earlier, weaker match, and reporting a
    confident, fully-verifying clean result over the wrong records while
    the real shortfall reappears, silently mis-signed, on the credit that
    got bumped. Tier 3 runs last over whatever is still unresolved, as one
    batched assignment.

    Credits are processed in id order in every phase so the claim set
    evolves identically on every run; results come back in input order.
    """
    ordered = sorted(credits, key=lambda c: c.id)
    utr_counts: dict[str, int] = {}
    for credit in ordered:
        if credit.utr:
            utr_counts[credit.utr] = utr_counts.get(credit.utr, 0) + 1
    shared_utrs = frozenset(utr for utr, count in utr_counts.items() if count > 1)

    # A batch whose UTR several credits name was split across those credits.
    # Tier 2 must search its parts, not the batch as one atom.
    split_batch_ids = frozenset(
        ref.id
        for utr in shared_utrs
        for ref in ledger.by_utr(utr)
        if ref.type is EntityType.SETTLEMENT_BATCH
    )

    proofs: dict[str, DecompositionProof] = {}
    declined_by_credit: dict[str, list[DecompositionReason]] = {}
    claimed: set[RecordRef] = set()

    # Phase 1: structural, over every credit, to completion, before any
    # credit is allowed to fall to tier 2.
    for credit in ordered:
        start_ns = clock()
        structural = _tier_structural(credit, ledger, frozenset(claimed), shared_utrs)
        if structural.unit is not None or structural.reason in _TERMINAL_REASONS:
            proof = _build_proof(
                credit,
                ledger,
                tier=DecompositionTier.STRUCTURAL,
                tiers_attempted=[DecompositionTier.STRUCTURAL],
                declined=[],
                result=structural,
                elapsed_ns=clock() - start_ns,
                calibration=calibration,
            )
            proofs[credit.id] = proof
            claimed.update(term.ref for term in proof.terms)
        else:
            declined_by_credit[credit.id] = [structural.reason] if structural.reason is not None else []

    # Phase 2: subset-sum, over every credit that didn't resolve
    # structurally, against the pool left over after phase 1 claimed
    # everything it could with certainty.
    for credit in ordered:
        if credit.id in proofs:
            continue
        start_ns = clock()
        subset, _units = _tier_subset_sum(
            credit, ledger, merchant_id, frozenset(claimed), split_batch_ids, budget, clock, start_ns
        )
        proof = _build_proof(
            credit,
            ledger,
            tier=DecompositionTier.SUBSET_SUM,
            tiers_attempted=[DecompositionTier.STRUCTURAL, DecompositionTier.SUBSET_SUM],
            declined=declined_by_credit[credit.id],
            result=subset,
            elapsed_ns=clock() - start_ns,
            calibration=calibration,
        )
        proofs[credit.id] = proof
        claimed.update(term.ref for term in proof.terms)

    escalating = [c for c in ordered if _may_escalate(proofs[c.id])]
    if escalating:
        start_ns = clock()
        # One shared pool: a unit near several credits must be contestable by
        # all of them, which is the whole reason this is an assignment and
        # not a per-credit nearest match.
        units = _assignment_pool(escalating, ledger, merchant_id, frozenset(claimed), split_batch_ids, budget)
        assignments = _tier_assignment(escalating, units, budget)
        for credit in escalating:
            previous = proofs[credit.id]
            result = assignments[credit.id]
            if result.unit is not None and set(result.unit.refs) & claimed:
                result = _TierResult(
                    reason=DecompositionReason.NO_ASSIGNMENT_FEASIBLE,
                    candidate_count=result.candidate_count,
                )
            # Why tier 2 gave up becomes part of this credit's history: the
            # proof should show the whole cascade, not just its last step.
            declined = list(previous.declined_reasons)
            if previous.reason is not None:
                declined.append(previous.reason)

            proof = _build_proof(
                credit,
                ledger,
                tier=DecompositionTier.ASSIGNMENT,
                tiers_attempted=[*previous.tiers_attempted, DecompositionTier.ASSIGNMENT],
                declined=declined,
                result=result,
                elapsed_ns=clock() - start_ns,
                calibration=calibration,
            )
            proofs[credit.id] = proof
            claimed.update(term.ref for term in proof.terms)

    return [proofs[credit.id] for credit in credits]


def _assignment_pool(
    credits: Sequence[BankCredit],
    ledger: Ledger,
    merchant_id: str,
    claimed: frozenset[RecordRef],
    split_batch_ids: frozenset[str],
    budget: DecompositionBudget,
) -> list[_Unit]:
    """Union of every escalating credit's candidate window, deduplicated by
    unit key and sorted, so the cost matrix's column order is fixed."""
    seen: dict[str, _Unit] = {}
    for credit in credits:
        units, _ = _candidate_units(credit, ledger, merchant_id, claimed, split_batch_ids, budget)
        for unit in units:
            seen.setdefault(unit.key, unit)
    return [seen[key] for key in sorted(seen)]


# --------------------------------------------------------------------------
# Independent verification
# --------------------------------------------------------------------------


class ProofIntegrityViolation(Exception):
    """Raised when verify_proof() finds a proof that does not check out
    against the ledger it was supposedly derived from -- a doubled
    citation, a tampered term, a hash mismatch. This is not a business
    exception like NoApplicableRule: it means the engine's own arithmetic
    cannot be trusted for this credit, so the run aborts rather than
    reporting a number built on it."""


def verify_proof(proof: DecompositionProof, ledger: Ledger) -> ProofVerification:
    """Re-derive a proof from the ledger alone.

    Takes no BankCredit: the credit is recovered through `credit_ref`. A
    verifier that accepted the credit from its caller could be handed a
    doctored one, and every arithmetic check below would pass.

    Each term's amount is recomputed from the cited record rather than
    trusted, which is what catches a tamper that leaves `sum_paise` intact
    (+1 on one term, -1 on another). The hash check catches the fields no
    arithmetic can: tier, outcome, reason, confidence.
    """
    failures: list[str] = []

    if proof.credit_ref.type is not EntityType.BANK_CREDIT:
        failures.append(f"credit_ref is a {proof.credit_ref.type.value}, not a bank_credit")
    credit = ledger.get(proof.credit_ref)
    if credit is None:
        failures.append(f"credit {proof.credit_ref.id} is not in the ledger")
    else:
        if credit.amount.paise != proof.credit_paise:
            failures.append(
                f"credit_paise {proof.credit_paise} != ledger amount {credit.amount.paise}"
            )
        if credit.amount.currency != proof.currency:
            failures.append(f"currency {proof.currency} != ledger currency {credit.amount.currency}")

    seen: set[RecordRef] = set()
    recomputed = 0
    for term in proof.terms:
        if term.ref in seen:
            failures.append(f"{term.ref.id} is cited more than once")
            continue
        seen.add(term.ref)
        record = ledger.get(term.ref)
        if record is None:
            failures.append(f"{term.ref.id} is not in the ledger")
            continue
        try:
            expected = signed_paise(record)
        except ValueError as exc:
            failures.append(f"{term.ref.id}: {exc}")
            continue
        if expected != term.signed_paise:
            failures.append(
                f"{term.ref.id}: proof says {term.signed_paise}, ledger says {expected}"
            )
        if credit is not None and money_of(record).currency != credit.amount.currency:
            failures.append(f"{term.ref.id} is not in {credit.amount.currency}")
        recomputed += expected

    ordered = sorted(proof.terms, key=lambda t: (t.ref.type.value, t.ref.id))
    if [t.ref for t in ordered] != [t.ref for t in proof.terms]:
        failures.append("terms are not in canonical order")

    if recomputed != proof.sum_paise:
        failures.append(f"sum_paise {proof.sum_paise} != recomputed {recomputed}")
    if proof.sum_paise + proof.residual_paise != proof.credit_paise:
        failures.append(
            f"sum_paise + residual_paise ({proof.sum_paise + proof.residual_paise}) "
            f"!= credit_paise ({proof.credit_paise})"
        )

    if proof.outcome is DecompositionOutcome.AMBIGUOUS:
        if proof.terms:
            failures.append("an ambiguous proof must not carry an answer")
        if proof.reason is None:
            failures.append("an ambiguous proof must name a reason")
        if not proof.competing:
            failures.append("an ambiguous proof must name its competitors")
        if proof.reason is DecompositionReason.MULTIPLE_SUBSETS_MATCH and len(proof.competing) < 2:
            failures.append("multiple_subsets_match must list at least two competitors")
    elif proof.outcome is DecompositionOutcome.UNRESOLVED:
        if proof.terms:
            failures.append("an unresolved proof must not carry an answer")
        if proof.reason is None:
            failures.append("an unresolved proof must name a reason")
    elif proof.reason is not None:
        failures.append(f"a resolved proof carries reason {proof.reason.value}")

    if proof.tier is DecompositionTier.STRUCTURAL and proof.outcome is DecompositionOutcome.RESOLVED:
        # The strongest check available, and only possible at tier 1: the
        # record set is re-derived from the ledger and must match exactly.
        expected_refs = _structural_refs(proof, ledger)
        if expected_refs is None:
            failures.append("structural proof does not resolve to a single batch")
        elif expected_refs != {t.ref for t in proof.terms}:
            failures.append("structural proof does not match the batch it claims")

    if proof.proof_hash != _hash_payload(proof):
        failures.append("proof_hash does not match the proof's own contents")

    return ProofVerification(ok=not failures, failures=failures)


def _structural_refs(proof: DecompositionProof, ledger: Ledger) -> set[RecordRef] | None:
    credit = ledger.get(proof.credit_ref)
    if credit is None or not credit.utr:
        return None
    batch_refs = [ref for ref in ledger.by_utr(credit.utr) if ref.type is EntityType.SETTLEMENT_BATCH]
    if len(batch_refs) != 1:
        return None
    return set(_batch_member_refs(ledger, batch_refs[0].id))
