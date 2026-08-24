"""Run-level guard on invariant 4.

Invariant 4 promises that the same inputs, contract version and seed produce
a byte-identical report. `core/decompose.py` keeps that promise everywhere
except one place, and this module is the check for that place.

Decomposition is bounded two ways. The **node budget** is a decision about
the search: the same inputs reach it at the same point on any machine, so a
run that exhausts it is still perfectly reproducible -- it just reproducibly
gives up. The **wall clock** is a decision about this machine on this day. A
faster box, or a quieter one, would have kept going and might have resolved
the credit. So a report containing even one `TIME_BUDGET_EXHAUSTED` carries
no byte-identical guarantee, and re-running it is not a valid check of
anything.

The subtlety worth stating plainly: nothing about a timed-out proof looks
wrong. It verifies, and its `proof_hash` is entirely self-consistent,
because the hash covers what the proof *says*, not what the machine was
doing while it said it. Only the reason code distinguishes "this is the
answer" from "this is the answer we had time for". That is why the check
lives here, at the level of a whole run, rather than inside `verify_proof`.

`eval/` is the only package allowed to read `datagen/` ground truth. This
module reads none -- it needs only the proofs -- but it belongs here because
it scores a run rather than computing one.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

from core.decompose import DecompositionProof, DecompositionReason

# The one reason that costs us reproducibility. Deliberately a set of one
# rather than an inline comparison: a future bound that also depends on the
# machine belongs in here, and the decision is easier to see as a list.
IRREPRODUCIBLE_REASONS = frozenset({DecompositionReason.TIME_BUDGET_EXHAUSTED})


class NonDeterministicRun(Exception):
    """Raised when a run cannot honour invariant 4."""


class ReproducibilityReport(BaseModel):
    total: int
    timed_out: list[str]
    node_bound: list[str]
    reproducible: bool


def check_reproducibility(proofs: Iterable[DecompositionProof]) -> ReproducibilityReport:
    """Which credits, if any, cost this run its byte-identical guarantee.

    `node_bound` is reported alongside but never counts against
    reproducibility. It is there because a run full of node-budget bailouts
    is a signal worth seeing -- the budget is probably too tight, or the
    candidate window too wide -- even though it is perfectly deterministic.
    """
    timed_out: list[str] = []
    node_bound: list[str] = []
    total = 0

    for proof in proofs:
        total += 1
        if proof.reason in IRREPRODUCIBLE_REASONS:
            timed_out.append(proof.credit_ref.id)
        elif proof.reason is DecompositionReason.NODE_BUDGET_EXHAUSTED:
            node_bound.append(proof.credit_ref.id)

    # Sorted so the report is itself reproducible, whatever order the proofs
    # arrived in.
    return ReproducibilityReport(
        total=total,
        timed_out=sorted(timed_out),
        node_bound=sorted(node_bound),
        reproducible=not timed_out,
    )


def assert_reproducible(proofs: Iterable[DecompositionProof]) -> ReproducibilityReport:
    """Gate a run on invariant 4, or refuse to score it.

    Raises rather than returning a flag because the alternative is an
    accuracy number computed over a run that cannot be reproduced, which is
    worse than no number: it looks like evidence.
    """
    report = check_reproducibility(proofs)
    if not report.reproducible:
        raise NonDeterministicRun(
            "invariant 4 (byte-identical reports) does not hold for this run: "
            f"{len(report.timed_out)} of {report.total} credits hit the wall-clock budget "
            f"({', '.join(report.timed_out)}). A slower or busier machine would have produced "
            "different decompositions, so this run must not be scored or published. "
            "Raise DecompositionBudget.time_budget_ns, or narrow the candidate window "
            "so the search finishes inside it."
        )
    return report
