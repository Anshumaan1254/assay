"""Tests for the LLM adjudication boundary.

llm/adjudicator.py handles what deterministic matching gave up on: a bank
credit whose decomposition proof still leaves a non-zero residual (any
outcome -- a structural hit can resolve and still not net exactly), an
AMBIGUOUS/UNRESOLVED proof (verify.py's explicit blind spot), and a ledger
record no proof claimed at all. It asks a model for ranked hypotheses, each
with mandatory citations restricted to a shown evidence pool, then never
trusts the model's own confidence -- coverage_bps is this module's own,
plain-Python arithmetic over the record IDs the hypothesis actually cited,
via core.conserve.signed_paise. lane is unconditionally PROPOSE: it
proposes, it never posts.

Written test-first per the working agreement. Fixtures are hand-built, not
run through decompose()/conserve() -- each test isolates exactly the piece
of the adjudicator it exercises, mirroring tests/test_verify.py's style.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from core.conserve import ConservationReport
from core.decompose import (
    CandidateSet,
    DecompositionOutcome,
    DecompositionProof,
    DecompositionReason,
    DecompositionTier,
    ProofTerm,
)
from core.exceptions import DiscrepancyClass
from core.ledger import Ledger
from core.models import (
    EntityType,
    FeeLine,
    FeeType,
    Lane,
    Payment,
    PaymentMethod,
    RecordRef,
    Refund,
)
from core.money import Money
from llm.adjudicator import (
    ADJUDICATION_BATCH_SIZE,
    DEFAULT_MODEL_HINT,
    AdjudicationBatchResponse,
    AdjudicationRejected,
    AdjudicationResult,
    AdjudicationRun,
    ResidualCase,
    ResidualKind,
    _coverage_bps,
    adjudicate_residuals,
    build_prompt,
    findings_from_adjudication_run,
    residuals_from_run,
)
from llm.provider import ProviderUnavailable
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider
from tests.support import RecordingProvider

MERCHANT = "MERCH-0001"
CAPTURED_AT = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
VALUE_DATE = date(2026, 7, 12)


# ---------------------------------------------------------------------------
# Local builders, matching tests/test_verify.py's style.
# ---------------------------------------------------------------------------


def _payment(id_="PAY-1", paise=500_000) -> Payment:
    return Payment(
        id=id_, merchant_id=MERCHANT, amount=Money(paise), method=PaymentMethod.CARD,
        network=None, card_type=None, is_international=False, mcc="5411",
        captured_at=CAPTURED_AT, settlement_id="STL-1",
    )


def _refund(id_="REF-1", payment_id="PAY-1", paise=100_000) -> Refund:
    return Refund(id=id_, payment_id=payment_id, amount=Money(paise), is_partial=False,
                   created_at=CAPTURED_AT, settlement_id="STL-1")


def _fee_line(id_="FEE-1", applies_to_id="PAY-1", paise=8_000) -> FeeLine:
    return FeeLine(
        id=id_, applies_to_id=applies_to_id, applies_to_type=EntityType.PAYMENT,
        fee_type=FeeType.MDR, computed_amount=Money(paise), rule_id="card.tier1",
    )


def _proof(
    terms: list[ProofTerm],
    credit_paise: int,
    credit_id="BC-1",
    outcome=DecompositionOutcome.RESOLVED,
    competing: list[CandidateSet] | None = None,
    reason: DecompositionReason | None = None,
) -> DecompositionProof:
    total = sum(t.signed_paise for t in terms)
    return DecompositionProof(
        credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id=credit_id),
        credit_paise=credit_paise, currency="INR",
        tier=DecompositionTier.STRUCTURAL, tiers_attempted=[DecompositionTier.STRUCTURAL],
        declined_reasons=[], outcome=outcome, reason=reason,
        terms=terms, sum_paise=total, residual_paise=credit_paise - total,
        competing=competing or [], confidence=10_000 if outcome is DecompositionOutcome.RESOLVED else 0,
        candidate_count=1, subset_size=len(terms), nodes_expanded=0,
        assignment_cost=None, assignment_margin=None, elapsed_ns=0, proof_hash="test-hash",
    )


def _report(proof: DecompositionProof) -> ConservationReport:
    """A ConservationReport consistent with `proof`'s own residual -- built
    by hand, not through conserve(), since these tests exercise the
    adjudicator's input-shaping, not the conservation identity itself."""
    return ConservationReport(
        credit_ref=proof.credit_ref, credit_paise=proof.credit_paise, currency=proof.currency,
        settled_gross_paise=proof.sum_paise, refunds_paise=0, fees_paise=0, tax_paise=0,
        chargebacks_paise=0, adjustments_paise=0, reversals_paise=0,
        unexplained_paise=proof.residual_paise,
    )


def _hypothesis_dict(discrepancy_class: str, cited: list[RecordRef], rationale="the pool explains it") -> dict:
    return {
        "discrepancy_class": discrepancy_class,
        "cited_evidence": [{"type": ref.type.value, "id": ref.id} for ref in cited],
        "rationale": rationale,
    }


def _batch_response(residual_id: str, hypotheses: list[dict]) -> dict:
    return {"results": [{"residual_id": residual_id, "hypotheses": hypotheses}]}


# ---------------------------------------------------------------------------
# the boundary itself
# ---------------------------------------------------------------------------


def test_the_batch_schema_is_handed_to_the_provider():
    payment = _payment()
    ledger = Ledger([payment])
    case = ResidualCase(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=5_000, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
        evidence_pool=[RecordRef(type=EntityType.PAYMENT, id="PAY-1")], context={},
    )
    provider = RecordingProvider(
        _batch_response("RES-BC-1", [_hypothesis_dict("unreconciled_residual", case.evidence_pool)])
    )

    adjudicate_residuals([case], ledger, provider)

    (prompt, schema, model_hint) = provider.calls[0]
    assert schema is AdjudicationBatchResponse
    assert model_hint == DEFAULT_MODEL_HINT
    assert "RES-BC-1" in prompt


def test_prompt_describes_the_task_not_the_json_shape():
    case = ResidualCase(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=5_000, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
        evidence_pool=[RecordRef(type=EntityType.PAYMENT, id="PAY-1")], context={},
    )
    prompt = build_prompt([case])

    assert "paise" in prompt
    for shape_talk in ("```json", '{"', "JSON object", "respond with json"):
        assert shape_talk.lower() not in prompt.lower()


def test_a_good_response_produces_accepted_hypotheses_with_valid_in_pool_citations():
    payment = _payment()
    ledger = Ledger([payment])
    pool = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    case = ResidualCase(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=500_000, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
        evidence_pool=pool, context={},
    )
    provider = RecordingProvider(
        _batch_response("RES-BC-1", [_hypothesis_dict("unreconciled_residual", pool)])
    )

    run = adjudicate_residuals([case], ledger, provider)

    assert run.results[0].accepted_hypotheses
    hyp = run.results[0].accepted_hypotheses[0]
    assert hyp.discrepancy_class is DiscrepancyClass.UNRECONCILED_RESIDUAL
    assert hyp.cited_evidence == pool


def test_provider_unavailable_propagates():
    case = ResidualCase(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=500_000, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
        evidence_pool=[RecordRef(type=EntityType.PAYMENT, id="PAY-1")], context={},
    )
    provider = RecordingProvider(ProviderUnavailable("rate limited past retries"))

    with pytest.raises(ProviderUnavailable):
        adjudicate_residuals([case], Ledger([_payment()]), provider)


def test_null_provider_propagates_provider_unavailable():
    case = ResidualCase(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=500_000, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
        evidence_pool=[RecordRef(type=EntityType.PAYMENT, id="PAY-1")], context={},
    )
    with pytest.raises(ProviderUnavailable):
        adjudicate_residuals([case], Ledger([_payment()]), NullProvider())


def test_adjudicator_does_not_import_the_gemini_sdk():
    source = Path("llm/adjudicator.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "google" not in imported
    assert "genai" not in imported


# ---------------------------------------------------------------------------
# reference-checking: existence AND pool membership
# ---------------------------------------------------------------------------


def test_citation_to_nonexistent_record_id_is_discarded_and_logged(capsys):
    payment = _payment()
    ledger = Ledger([payment])
    pool = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    fabricated = RecordRef(type=EntityType.PAYMENT, id="PAY-DOES-NOT-EXIST")
    case = ResidualCase(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=500_000, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
        evidence_pool=pool, context={},
    )
    provider = RecordingProvider(
        _batch_response("RES-BC-1", [_hypothesis_dict("unreconciled_residual", [fabricated])])
    )

    run = adjudicate_residuals([case], ledger, provider)

    assert run.results[0].accepted_hypotheses == []
    assert run.results[0].rejected_count == 1
    logged = capsys.readouterr().out
    assert "adjudication_hypothesis_rejected" in logged
    assert "PAY-DOES-NOT-EXIST" in logged


def test_citation_outside_the_shown_evidence_pool_is_discarded():
    payment = _payment()
    other_payment = _payment(id_="PAY-2")
    ledger = Ledger([payment, other_payment])
    pool = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    not_shown = RecordRef(type=EntityType.PAYMENT, id="PAY-2")  # real, but not in this residual's pool
    case = ResidualCase(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=500_000, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
        evidence_pool=pool, context={},
    )
    provider = RecordingProvider(
        _batch_response("RES-BC-1", [_hypothesis_dict("unreconciled_residual", [not_shown])])
    )

    run = adjudicate_residuals([case], ledger, provider)

    assert run.results[0].accepted_hypotheses == []
    assert run.results[0].rejected_count == 1


def test_hypothesis_with_zero_valid_citations_is_fully_discarded():
    ledger = Ledger([_payment()])
    pool = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    case = ResidualCase(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=500_000, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
        evidence_pool=pool, context={},
    )
    bad = RecordRef(type=EntityType.PAYMENT, id="NOPE")
    provider = RecordingProvider(_batch_response("RES-BC-1", [_hypothesis_dict("unreconciled_residual", [bad])]))

    run = adjudicate_residuals([case], ledger, provider)

    assert run.results[0].accepted_hypotheses == []


# ---------------------------------------------------------------------------
# _coverage_bps: the deterministic, generalizable re-verification rule
# ---------------------------------------------------------------------------


def test_coverage_bps_full_credit_when_citations_sum_exactly_to_residual():
    ledger = Ledger([_payment(paise=500_000)])
    refs = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    assert _coverage_bps(refs, 500_000, ledger) == 10_000


def test_coverage_bps_zero_when_citations_net_the_wrong_sign():
    ledger = Ledger([_payment(paise=500_000)])  # PAYMENT contributes +500_000
    refs = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    assert _coverage_bps(refs, -500_000, ledger) == 0  # residual wants a negative explanation


def test_coverage_bps_partial_and_capped_at_10000():
    ledger = Ledger([_payment(id_="PAY-1", paise=250_000), _payment(id_="PAY-2", paise=1_000_000)])
    partial_refs = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    assert _coverage_bps(partial_refs, 500_000, ledger) == 5_000  # 250_000 / 500_000

    overexplaining_refs = [RecordRef(type=EntityType.PAYMENT, id="PAY-2")]
    assert _coverage_bps(overexplaining_refs, 500_000, ledger) == 10_000  # capped, not 20_000


# ---------------------------------------------------------------------------
# empty-pool residuals are never sent to the model
# ---------------------------------------------------------------------------


def test_residual_with_empty_evidence_pool_is_skipped_without_calling_the_model():
    credit_ref = RecordRef(type=EntityType.BANK_CREDIT, id="BC-1")
    case = ResidualCase(
        residual_id="RES-BC-1", credit_ref=credit_ref, residual_paise=500_000, currency="INR",
        kind=ResidualKind.DECOMPOSITION_RESIDUAL, evidence_pool=[], context={},
    )
    provider = RecordingProvider(_batch_response("RES-BC-1", []))

    run = adjudicate_residuals([case], Ledger([]), provider)

    assert provider.calls == []
    assert run.skipped_no_evidence == [credit_ref]
    assert run.results == []


# ---------------------------------------------------------------------------
# batching
# ---------------------------------------------------------------------------


def test_batching_splits_many_residuals_into_bounded_calls_and_api_call_count_matches():
    payment = _payment()
    ledger = Ledger([payment])
    pool = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    cases = [
        ResidualCase(
            residual_id=f"RES-BC-{i}", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id=f"BC-{i}"),
            residual_paise=1_000, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
            evidence_pool=pool, context={},
        )
        for i in range(ADJUDICATION_BATCH_SIZE * 2 + 3)  # forces 3 batches
    ]
    call_batches: list[list[ResidualCase]] = [
        cases[start : start + ADJUDICATION_BATCH_SIZE] for start in range(0, len(cases), ADJUDICATION_BATCH_SIZE)
    ]
    state = {"n": 0}

    def responder(prompt, schema, model_hint):
        batch = call_batches[state["n"]]
        state["n"] += 1
        return {
            "results": [
                {"residual_id": c.residual_id, "hypotheses": [_hypothesis_dict("unreconciled_residual", pool)]}
                for c in batch
            ]
        }

    provider = RecordingProvider(responder)
    run = adjudicate_residuals(cases, ledger, provider)

    assert len(provider.calls) == 3
    assert run.api_call_count == 3
    assert run.residuals_submitted == len(cases)
    assert len(run.results) == len(cases)


def test_a_schema_invalid_batch_response_raises_adjudication_rejected_and_is_logged(capsys):
    ledger = Ledger([_payment()])
    pool = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    case = ResidualCase(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=500_000, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
        evidence_pool=pool, context={},
    )
    provider = RecordingProvider({"results": [{"residual_id": "RES-BC-1", "hypotheses": []}]})  # min_length=1 violated

    with pytest.raises(AdjudicationRejected):
        adjudicate_residuals([case], ledger, provider)

    logged = capsys.readouterr().out
    assert "adjudication_batch_rejected" in logged
    assert "RES-BC-1" in logged


# ---------------------------------------------------------------------------
# Finding construction
# ---------------------------------------------------------------------------


def test_findings_from_adjudication_run_emits_nothing_for_a_fully_rejected_residual():
    result = AdjudicationResult(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=5_000, currency="INR", accepted_hypotheses=[], rejected_count=2,
    )
    run = AdjudicationRun(results=[result], skipped_no_evidence=[], api_call_count=1, residuals_submitted=1)

    assert findings_from_adjudication_run(run, audit_run_id="RUN-1") == []


def test_findings_from_adjudication_run_lane_is_always_propose_never_auto():
    result = AdjudicationResult(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=5_000, currency="INR",
        accepted_hypotheses=[
            {
                "discrepancy_class": DiscrepancyClass.UNRECONCILED_RESIDUAL,
                "cited_evidence": [RecordRef(type=EntityType.PAYMENT, id="PAY-1")],
                "rationale": "matches",
                "coverage_bps": 10_000,
                "rank": 1,
            }
        ],
        rejected_count=0,
    )
    run = AdjudicationRun(results=[result], skipped_no_evidence=[], api_call_count=1, residuals_submitted=1)

    findings = findings_from_adjudication_run(run, audit_run_id="RUN-1")

    assert len(findings) == 1
    assert findings[0].lane is Lane.PROPOSE


def test_findings_from_adjudication_run_amount_impact_equals_residual_magnitude():
    result = AdjudicationResult(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=-5_000, currency="INR",
        accepted_hypotheses=[
            {
                "discrepancy_class": DiscrepancyClass.UNRECONCILED_RESIDUAL,
                "cited_evidence": [RecordRef(type=EntityType.PAYMENT, id="PAY-1")],
                "rationale": "matches",
                "coverage_bps": 7_500,
                "rank": 1,
            }
        ],
        rejected_count=0,
    )
    run = AdjudicationRun(results=[result], skipped_no_evidence=[], api_call_count=1, residuals_submitted=1)

    findings = findings_from_adjudication_run(run, audit_run_id="RUN-1")

    assert findings[0].amount_impact == Money(5_000)
    assert findings[0].confidence == 7_500


def test_findings_from_adjudication_run_ids_are_deterministic_and_collision_free():
    def _result(residual_id: str) -> AdjudicationResult:
        return AdjudicationResult(
            residual_id=residual_id, credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id=residual_id),
            residual_paise=5_000, currency="INR",
            accepted_hypotheses=[
                {
                    "discrepancy_class": DiscrepancyClass.UNRECONCILED_RESIDUAL,
                    "cited_evidence": [RecordRef(type=EntityType.PAYMENT, id="PAY-1")],
                    "rationale": "matches",
                    "coverage_bps": 10_000,
                    "rank": 1,
                },
                {
                    "discrepancy_class": DiscrepancyClass.ROUNDING_DRIFT,
                    "cited_evidence": [RecordRef(type=EntityType.PAYMENT, id="PAY-1")],
                    "rationale": "or maybe this",
                    "coverage_bps": 5_000,
                    "rank": 2,
                },
            ],
            rejected_count=0,
        )

    run = AdjudicationRun(
        results=[_result("RES-BC-1"), _result("RES-BC-2")],
        skipped_no_evidence=[], api_call_count=1, residuals_submitted=2,
    )

    first = findings_from_adjudication_run(run, audit_run_id="RUN-X")
    ids = [f.id for f in first]
    assert len(ids) == len(set(ids)) == 2  # 2 residuals, one Finding each -- not one per hypothesis

    second = findings_from_adjudication_run(run, audit_run_id="RUN-X")
    assert [f.id for f in second] == ids


def test_findings_from_adjudication_run_never_double_counts_a_residual_across_hypotheses():
    """Regression test: emitting one Finding per accepted hypothesis would
    let a single residual's amount be claimed multiple times the moment
    anything sums amount_impact over the returned list -- the natural
    thing a report does. Multiple accepted hypotheses must still produce
    exactly one Finding, from the top-ranked (lowest rank) hypothesis."""
    result = AdjudicationResult(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=5_000, currency="INR",
        accepted_hypotheses=[
            {
                "discrepancy_class": DiscrepancyClass.ROUNDING_DRIFT,
                "cited_evidence": [RecordRef(type=EntityType.PAYMENT, id="PAY-1")],
                "rationale": "runner-up, listed second in the response",
                "coverage_bps": 5_000,
                "rank": 2,
            },
            {
                "discrepancy_class": DiscrepancyClass.UNRECONCILED_RESIDUAL,
                "cited_evidence": [RecordRef(type=EntityType.PAYMENT, id="PAY-2")],
                "rationale": "top pick",
                "coverage_bps": 10_000,
                "rank": 1,
            },
        ],
        rejected_count=0,
    )
    run = AdjudicationRun(results=[result], skipped_no_evidence=[], api_call_count=1, residuals_submitted=1)

    findings = findings_from_adjudication_run(run, audit_run_id="RUN-1")

    assert len(findings) == 1
    assert sum((f.amount_impact for f in findings), start=Money(0)) == Money(5_000)
    assert findings[0].discrepancy_class is DiscrepancyClass.UNRECONCILED_RESIDUAL  # rank 1, not list order
    assert findings[0].confidence == 10_000
    assert findings[0].evidence_ids == [RecordRef(type=EntityType.PAYMENT, id="PAY-2")]


# ---------------------------------------------------------------------------
# caching -- the existing CachedProvider, not a second cache
# ---------------------------------------------------------------------------


def _one_case() -> ResidualCase:
    return ResidualCase(
        residual_id="RES-BC-1", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-1"),
        residual_paise=500_000, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
        evidence_pool=[RecordRef(type=EntityType.PAYMENT, id="PAY-1")], context={},
    )


def test_a_cache_hit_never_reaches_the_inner_provider(tmp_path: Path):
    pool = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    inner = RecordingProvider(_batch_response("RES-BC-1", [_hypothesis_dict("unreconciled_residual", pool)]))
    cached = CachedProvider(inner, cache_dir=tmp_path)
    ledger = Ledger([_payment()])
    case = _one_case()

    adjudicate_residuals([case], ledger, cached)
    adjudicate_residuals([case], ledger, cached)

    assert len(inner.calls) == 1


def test_a_replay_from_cache_works_with_a_dead_provider(tmp_path: Path):
    pool = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    inner = RecordingProvider(_batch_response("RES-BC-1", [_hypothesis_dict("unreconciled_residual", pool)]))
    ledger = Ledger([_payment()])
    case = _one_case()
    CachedProvider(inner, cache_dir=tmp_path).generate_structured(build_prompt([case]), AdjudicationBatchResponse, DEFAULT_MODEL_HINT)

    offline = CachedProvider(NullProvider(), cache_dir=tmp_path)
    run = adjudicate_residuals([case], ledger, offline)
    assert run.results[0].accepted_hypotheses


def test_a_changed_input_misses_the_cache(tmp_path: Path):
    pool = [RecordRef(type=EntityType.PAYMENT, id="PAY-1")]
    inner = RecordingProvider(_batch_response("RES-BC-1", [_hypothesis_dict("unreconciled_residual", pool)]))
    cached = CachedProvider(inner, cache_dir=tmp_path)
    ledger = Ledger([_payment()])
    case = _one_case()
    other_case = ResidualCase(
        residual_id="RES-BC-2", credit_ref=RecordRef(type=EntityType.BANK_CREDIT, id="BC-2"),
        residual_paise=999, currency="INR", kind=ResidualKind.DECOMPOSITION_RESIDUAL,
        evidence_pool=pool, context={},
    )

    adjudicate_residuals([case], ledger, cached)
    adjudicate_residuals([other_case], ledger, cached)

    assert len(inner.calls) == 2


# ---------------------------------------------------------------------------
# residuals_from_run: reshaping pipeline outputs into ResidualCases
# ---------------------------------------------------------------------------


def test_residuals_from_run_builds_one_case_per_nonzero_conservation_residual():
    payment = _payment(paise=500_000)
    fee = _fee_line(paise=9_000)
    ledger = Ledger([payment, fee])
    resolved_terms = [
        ProofTerm(ref=RecordRef(type=EntityType.PAYMENT, id="PAY-1"), signed_paise=500_000),
        ProofTerm(ref=RecordRef(type=EntityType.FEE_LINE, id="FEE-1"), signed_paise=-9_000),
    ]
    proof = _proof(resolved_terms, credit_paise=500_000 - 9_000 - 500)  # leaves a residual
    report = _report(proof)

    build = residuals_from_run([proof], [report], ledger)

    assert len(build.cases) == 1
    case = build.cases[0]
    assert case.kind is ResidualKind.DECOMPOSITION_RESIDUAL
    assert case.residual_paise == report.unexplained_paise
    assert set(case.evidence_pool) == {t.ref for t in resolved_terms}
    assert build.skipped_no_evidence == []


def test_residuals_from_run_builds_one_case_per_unclaimed_record():
    payment = _payment(paise=500_000)
    ledger = Ledger([payment])
    unclaimed_ref = RecordRef(type=EntityType.PAYMENT, id="PAY-1")

    build = residuals_from_run([], [], ledger, unclaimed=[unclaimed_ref])

    assert len(build.cases) == 1
    case = build.cases[0]
    assert case.kind is ResidualKind.UNCLAIMED_RECORD
    assert case.credit_ref is None
    assert case.residual_paise == 500_000
    assert unclaimed_ref in case.evidence_pool


def test_residuals_from_run_reports_empty_pool_residuals_as_skipped_not_dropped_silently():
    proof = _proof([], credit_paise=100_000, outcome=DecompositionOutcome.UNRESOLVED,
                    reason=DecompositionReason.NO_CANDIDATES_IN_WINDOW)
    report = _report(proof)

    build = residuals_from_run([proof], [report], Ledger([]))

    assert build.cases == []
    assert build.skipped_no_evidence == [proof.credit_ref]
