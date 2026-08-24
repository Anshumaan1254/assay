"""For residuals deterministic matching could not explain, propose a root-cause
hypothesis with mandatory citations. Proposes; never posts.

This module is deliberately thin, and deliberately untrusting -- the same
discipline as llm/contract_parser.py, applied to a different boundary.

It never sees a vendor SDK: it takes an `LLMProvider` and hands it
`AdjudicationBatchResponse`, so the provider can constrain decoding to that
schema. The prompt describes the investigation task and says nothing about
field names.

Three residual shapes reach this module, none of which anything else in the
engine turns into a Finding today:

  - a `ConservationReport` whose `unexplained_paise` is non-zero, for ANY
    proof outcome -- a structural (tier-1) hit "never cascades"
    (core/decompose.py), so a resolved proof can still leave a residual;
    verify.py only recomputes fee/tax/chargeback lines on RESOLVED proofs
    and never looks at this residual at all.
  - an AMBIGUOUS/UNRESOLVED `DecompositionProof` -- verify.py's explicit
    blind spot, since `terms` is empty by construction.
  - a `RecordRef` from `core.conserve.unclaimed_records()` -- a record no
    proof claimed at all (e.g. a whole settlement batch whose bank credit
    never arrived).

A hypothesis may cite ONLY records shown in its residual's own evidence
pool -- existence in the ledger is necessary but not sufficient; a citation
to a real record the model was never shown is rejected exactly like a
citation to a record that does not exist at all. This module never trusts
the model's own confidence, because none is ever asked for: `coverage_bps`
is computed here, in plain Python, from how much of the residual a
hypothesis's own (valid) citations actually net to via
`core.conserve.signed_paise` -- the same generalizable rule for every
`DiscrepancyClass`, with no per-class special-casing. That integer becomes
`Finding.confidence`; `Finding.lane` is unconditionally `PROPOSE`.

Caching is not implemented here, for the same reason it isn't in
contract_parser.py: `llm/providers/cached.py` already keys a disk cache on
SHA-256 of (prompt + schema + model); wrapping a provider in it is all this
needs.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

import structlog
from pydantic import BaseModel, Field, ValidationError

from core.conserve import ConservationReport, money_of, signed_paise
from core.decompose import DecompositionOutcome, DecompositionProof
from core.exceptions import DiscrepancyClass
from core.ledger import Ledger
from core.models import EntityType, Finding, Lane, Money, RecordRef, Severity
from llm.provider import LLMProvider

logger = structlog.get_logger(__name__)

DEFAULT_MODEL_HINT = "flash"
ADJUDICATION_BATCH_SIZE = 8
MAX_EVIDENCE_POOL = 25


class AdjudicationRejected(Exception):
    """A batch's response failed schema validation. Propagates -- no
    partial-batch repair, mirroring ContractParseRejected: a bad answer for
    even one residual in a batch means the whole batch produced nothing."""


# ---------------------------------------------------------------------------
# Residual input shapes
# ---------------------------------------------------------------------------


class ResidualKind(StrEnum):
    DECOMPOSITION_RESIDUAL = "decomposition_residual"
    UNCLAIMED_RECORD = "unclaimed_record"


class ResidualCase(BaseModel):
    """One thing to investigate. Not an `AssayModel`: this is an `llm/`-facing
    transport shape assembled from already-computed pipeline outputs, never
    persisted or cited by id itself."""

    residual_id: str
    credit_ref: RecordRef | None
    residual_paise: int
    currency: str
    kind: ResidualKind
    evidence_pool: list[RecordRef]
    context: dict[str, str | int]


class ResidualBuildResult(BaseModel):
    cases: list[ResidualCase]
    skipped_no_evidence: list[RecordRef]


def _decomposition_evidence_pool(proof: DecompositionProof) -> list[RecordRef]:
    if proof.outcome is DecompositionOutcome.RESOLVED:
        refs = [term.ref for term in proof.terms]
    elif proof.outcome is DecompositionOutcome.AMBIGUOUS:
        seen: set[RecordRef] = set()
        refs = []
        for candidate in proof.competing:
            for ref in candidate.refs:
                if ref not in seen:
                    seen.add(ref)
                    refs.append(ref)
    else:
        refs = []
    if not refs:
        return []
    return sorted(set(refs), key=lambda r: (r.type.value, r.id))[:MAX_EVIDENCE_POOL]


def _unclaimed_evidence_pool(ledger: Ledger, ref: RecordRef) -> list[RecordRef]:
    if ref.type is not EntityType.PAYMENT:
        return [ref]
    pool = [ref]
    for fee_ref in ledger.fee_lines_for(ref):
        pool.append(fee_ref)
        pool.extend(ledger.tax_lines_for(fee_ref))
    return sorted(set(pool), key=lambda r: (r.type.value, r.id))[:MAX_EVIDENCE_POOL]


def residuals_from_run(
    proofs: Sequence[DecompositionProof],
    reports: Sequence[ConservationReport],
    ledger: Ledger,
    *,
    unclaimed: Sequence[RecordRef] = (),
) -> ResidualBuildResult:
    """Reshape already-computed decompose/conserve output into residuals the
    adjudicator can investigate. Does not call decompose/verify/conserve
    itself -- no new orchestration, exactly like parse_rate_card takes a
    plain `document: str` rather than fetching one."""
    proof_by_credit_ref = {proof.credit_ref: proof for proof in proofs}
    cases: list[ResidualCase] = []
    skipped: list[RecordRef] = []

    for report in sorted(reports, key=lambda r: (r.credit_ref.type.value, r.credit_ref.id)):
        if report.unexplained_paise == 0:
            continue
        proof = proof_by_credit_ref.get(report.credit_ref)
        if proof is None:
            raise ValueError(f"{report.credit_ref.id}: no matching proof was supplied")
        pool = _decomposition_evidence_pool(proof)
        if not pool:
            # Nothing to cite: forcing a call would either invite a
            # hallucinated citation or a free-text non-answer. Reported so
            # the caller can still see this residual exists.
            skipped.append(report.credit_ref)
            continue
        cases.append(
            ResidualCase(
                residual_id=f"RES-{report.credit_ref.id}",
                credit_ref=report.credit_ref,
                residual_paise=report.unexplained_paise,
                currency=report.currency,
                kind=ResidualKind.DECOMPOSITION_RESIDUAL,
                evidence_pool=pool,
                context={
                    "tier": proof.tier.value,
                    "outcome": proof.outcome.value,
                    "reason": proof.reason.value if proof.reason is not None else "",
                    "candidate_count": proof.candidate_count,
                },
            )
        )

    for ref in sorted(unclaimed, key=lambda r: (r.type.value, r.id)):
        record = ledger.get(ref)
        if record is None:
            raise ValueError(f"{ref.id}: unclaimed record is not in the ledger")
        cases.append(
            ResidualCase(
                residual_id=f"RES-UNCLAIMED-{ref.id}",
                credit_ref=None,
                residual_paise=signed_paise(record),
                currency=money_of(record).currency,
                kind=ResidualKind.UNCLAIMED_RECORD,
                evidence_pool=_unclaimed_evidence_pool(ledger, ref),
                context={"record_type": ref.type.value},
            )
        )

    return ResidualBuildResult(cases=cases, skipped_no_evidence=skipped)


# ---------------------------------------------------------------------------
# LLM wire schema
# ---------------------------------------------------------------------------


class HypothesisCandidate(BaseModel):
    discrepancy_class: DiscrepancyClass
    cited_evidence: list[RecordRef] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=500)


class ResidualHypotheses(BaseModel):
    residual_id: str
    hypotheses: list[HypothesisCandidate] = Field(min_length=1, max_length=3)


class AdjudicationBatchResponse(BaseModel):
    results: list[ResidualHypotheses]


_PROMPT = """You are investigating settlement discrepancies a deterministic reconciliation
engine could not explain on its own: bank credits (or individual ledger records) whose
amount does not net against the transactions the engine could positively match to them.

For each residual below, you are given the exact unexplained amount in paise (1 rupee is
100 paise) and a bounded pool of candidate records -- payments, refunds, chargebacks,
adjustments, fee lines, and tax lines -- that were near this residual in time or amount.
You may cite ONLY records from that residual's own pool; a citation to any other record id
will be discarded, and a hypothesis left with no valid citation will be discarded entirely.

For each residual, propose up to three ranked hypotheses for what happened, most likely
first. Each hypothesis names one discrepancy class from the fixed taxonomy below, cites the
specific records from that residual's pool that support it, and gives a short rationale
grounded in those records' actual attributes -- amounts, ids, timing -- not speculation
about causes the pool gives no evidence for.

Discrepancy classes: {taxonomy}

Residuals:
{residuals}
"""


def _render_residual(case: ResidualCase) -> str:
    pool_lines = "\n".join(f"  - {ref.type.value} {ref.id}" for ref in case.evidence_pool) or "  (none)"
    context_lines = "\n".join(f"  {key}: {value}" for key, value in sorted(case.context.items())) or "  (none)"
    return (
        f"Residual {case.residual_id} ({case.kind.value}):\n"
        f"  unexplained amount: {case.residual_paise} paise\n"
        f"  candidate records:\n{pool_lines}\n"
        f"  context:\n{context_lines}\n"
    )


def build_prompt(batch: Sequence[ResidualCase]) -> str:
    taxonomy = ", ".join(member.value for member in DiscrepancyClass)
    residuals = "\n".join(_render_residual(case) for case in batch)
    return _PROMPT.format(taxonomy=taxonomy, residuals=residuals)


# ---------------------------------------------------------------------------
# Deterministic arithmetic re-verification -- one rule, every DiscrepancyClass
# ---------------------------------------------------------------------------


def _coverage_bps(valid_cited: Sequence[RecordRef], residual_paise: int, ledger: Ledger) -> int:
    """How much of `residual_paise` the hypothesis's OWN valid citations
    arithmetically explain, via signed_paise -- computed here, never by the
    model. 0 when unsupported or wrong-signed; otherwise capped at 10_000.
    """
    if residual_paise == 0:
        return 0
    explained = 0
    for ref in valid_cited:
        record = ledger.get(ref)
        try:
            explained += signed_paise(record)
        except ValueError:
            continue  # a valid but non-contributing citation (e.g. the credit itself): 0 weight
    if explained == 0 or (explained > 0) != (residual_paise > 0):
        return 0
    capped = min(abs(explained), abs(residual_paise))
    return 10_000 * capped // abs(residual_paise)


# ---------------------------------------------------------------------------
# Boundary functions
# ---------------------------------------------------------------------------


class AdjudicatedHypothesis(BaseModel):
    discrepancy_class: DiscrepancyClass
    cited_evidence: list[RecordRef]
    rationale: str
    coverage_bps: int
    rank: int


class AdjudicationResult(BaseModel):
    residual_id: str
    credit_ref: RecordRef | None
    residual_paise: int
    currency: str
    accepted_hypotheses: list[AdjudicatedHypothesis]
    rejected_count: int


class AdjudicationRun(BaseModel):
    results: list[AdjudicationResult]
    skipped_no_evidence: list[RecordRef]
    api_call_count: int
    residuals_submitted: int


def _citation_rejection_reason(ref: RecordRef, pool: frozenset[RecordRef], ledger: Ledger) -> str | None:
    if not ledger.exists(ref):
        return "record does not exist in the ledger"
    if ref not in pool:
        return "record exists but was not shown to the model for this residual"
    return None


def _process_hypothesis(
    candidate: HypothesisCandidate,
    case: ResidualCase,
    pool: frozenset[RecordRef],
    ledger: Ledger,
    rank: int,
) -> AdjudicatedHypothesis | None:
    valid: list[RecordRef] = []
    for ref in candidate.cited_evidence:
        reason = _citation_rejection_reason(ref, pool, ledger)
        if reason is not None:
            logger.warning(
                "adjudication_hypothesis_rejected",
                residual_id=case.residual_id,
                discrepancy_class=candidate.discrepancy_class.value,
                cited_ref=f"{ref.type.value}:{ref.id}",
                invalid_reason=reason,
            )
            continue
        valid.append(ref)
    if not valid:
        return None
    return AdjudicatedHypothesis(
        discrepancy_class=candidate.discrepancy_class,
        cited_evidence=valid,
        rationale=candidate.rationale,
        coverage_bps=_coverage_bps(valid, case.residual_paise, ledger),
        rank=rank,
    )


def adjudicate_residuals(
    cases: Sequence[ResidualCase],
    ledger: Ledger,
    provider: LLMProvider,
    *,
    model_hint: str = DEFAULT_MODEL_HINT,
    batch_size: int = ADJUDICATION_BATCH_SIZE,
    skipped_no_evidence: Sequence[RecordRef] = (),
) -> AdjudicationRun:
    """Adjudicate every case with a non-empty evidence pool, batching
    `batch_size` residuals per `generate_structured` call.

    `ProviderUnavailable` is allowed to propagate untouched: an absent model
    means no hypotheses, never a guessed one. A schema-invalid batch
    response raises `AdjudicationRejected` and also aborts the run -- no
    partial-batch repair.
    """
    sendable: list[ResidualCase] = []
    skipped: list[RecordRef] = list(skipped_no_evidence)
    for case in cases:
        if case.evidence_pool:
            sendable.append(case)
        elif case.credit_ref is not None:
            skipped.append(case.credit_ref)
        else:
            raise ValueError(f"{case.residual_id}: empty evidence pool with no credit_ref to report it under")

    results: list[AdjudicationResult] = []
    api_call_count = 0

    for start in range(0, len(sendable), batch_size):
        batch = sendable[start : start + batch_size]
        prompt = build_prompt(batch)
        raw = provider.generate_structured(prompt, AdjudicationBatchResponse, model_hint)
        api_call_count += 1

        try:
            response = AdjudicationBatchResponse.model_validate(raw)
        except ValidationError as error:
            residual_ids = [case.residual_id for case in batch]
            logger.warning(
                "adjudication_batch_rejected",
                residual_ids=residual_ids,
                model_hint=model_hint,
                error_count=len(error.errors()),
            )
            raise AdjudicationRejected(
                f"the model's batch response was rejected and no hypotheses were produced for "
                f"residuals {residual_ids}: {error}"
            ) from error

        response_by_id = {r.residual_id: r for r in response.results}
        for case in batch:
            pool = frozenset(case.evidence_pool)
            hypotheses = response_by_id.get(case.residual_id)
            accepted: list[AdjudicatedHypothesis] = []
            rejected_count = 0
            if hypotheses is not None:
                for rank, candidate in enumerate(hypotheses.hypotheses, start=1):
                    processed = _process_hypothesis(candidate, case, pool, ledger, rank)
                    if processed is None:
                        rejected_count += 1
                    else:
                        accepted.append(processed)
            results.append(
                AdjudicationResult(
                    residual_id=case.residual_id,
                    credit_ref=case.credit_ref,
                    residual_paise=case.residual_paise,
                    currency=case.currency,
                    accepted_hypotheses=accepted,
                    rejected_count=rejected_count,
                )
            )

    return AdjudicationRun(
        results=results,
        skipped_no_evidence=sorted(skipped, key=lambda r: (r.type.value, r.id)),
        api_call_count=api_call_count,
        residuals_submitted=len(sendable),
    )


# ---------------------------------------------------------------------------
# Finding construction -- lane is always PROPOSE, never AUTO
# ---------------------------------------------------------------------------


class _IdSeq:
    """Deterministic, collision-free Finding ids, mirroring core/verify.py's
    _IdSeq: one residual can yield several accepted hypotheses, each its own
    Finding, and every caller here iterates in a fixed sorted order, so a
    given run produces the same ids on every replay."""

    def __init__(self, audit_run_id: str, residual_id: str):
        self._prefix = f"FND-ADJ-{audit_run_id}-{residual_id}"
        self._n = 0

    def next(self) -> str:
        self._n += 1
        return f"{self._prefix}-{self._n:03d}"


def findings_from_adjudication_run(run: AdjudicationRun, audit_run_id: str) -> list[Finding]:
    """One Finding per accepted hypothesis, not just the top-ranked one --
    mirrors decompose.py's refusal to silently collapse competing
    explanations. `lane` is unconditionally PROPOSE: "it proposes, it never
    posts" is structural here, not advisory. `confidence` is `coverage_bps`,
    a deterministically Python-computed integer -- never the model's own
    self-report -- so invariant 2 holds even though a number ends up on the
    Finding.
    """
    findings: list[Finding] = []
    for result in sorted(run.results, key=lambda r: r.residual_id):
        if not result.accepted_hypotheses:
            continue
        ids = _IdSeq(audit_run_id, result.residual_id)
        amount = Money(abs(result.residual_paise), result.currency)
        for hypothesis in sorted(result.accepted_hypotheses, key=lambda h: h.rank):
            findings.append(
                Finding(
                    id=ids.next(),
                    audit_run_id=audit_run_id,
                    discrepancy_class=hypothesis.discrepancy_class,
                    severity=Severity.MAJOR,
                    amount_impact=amount,
                    evidence_ids=hypothesis.cited_evidence,
                    confidence=hypothesis.coverage_bps,
                    lane=Lane.PROPOSE,
                    explanation=(
                        f"[adjudicator hypothesis, {hypothesis.coverage_bps} bps arithmetic coverage] "
                        f"{hypothesis.rationale}"
                    ),
                )
            )
    return findings
