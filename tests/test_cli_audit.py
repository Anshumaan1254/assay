"""Tests for cli/audit.py -- the end-to-end audit runner.

Money-critical, and written test-first per the working agreement. This is
the module that turns eight independently-tested engine functions into one
run, so the things worth pinning here are the properties that only exist at
the level of a whole run and cannot be checked inside any single function:

  - invariant 3, asserted at the end of the run over BOTH halves of the
    accounting (residual on credits that exist, plus records no credit ever
    claimed) -- core/conserve.py deliberately splits these, and a runner
    that reported only the first would call a run clean while a whole lost
    settlement batch sat unclaimed;
  - invariant 4, as an actual byte comparison of two consecutive
    `report_hash` values rather than an argument that it should hold;
  - invariant 7, as a re-run posting zero new journal entries;
  - the degraded path: no model available still produces a number.

The committed `runs/realistic-seed42` is used as the integration fixture
throughout, the same as tests/test_verify.py's own integration tests, and
the contract is compiled through `CachedProvider(NullProvider())` so every
test here runs offline off the committed cache.
"""

from __future__ import annotations

import json
from pathlib import Path
from random import Random

import pytest

from cli.audit import AuditReport, run_audit
from cli.loaders import load_ledger
from core.conserve import total_unexplained_paise, unclaimed_paise
from core.models import Lane
from datagen.config import GenerationConfig, load_profile
from datagen.inject import apply_discrepancies
from datagen.ratecard import default_rate_card
from datagen.world import build_true_world
from datagen.writer import write_run
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMITTED_RUN = REPO_ROOT / "runs" / "realistic-seed42"
MERCHANT = "MERCH-0001"


ADJUDICATED_PREFIX = "FND-ADJ-"


def _offline_provider() -> CachedProvider:
    """Every LLM call in these tests goes through the committed disk cache,
    so the whole file runs with no network and no API key."""
    return CachedProvider(NullProvider())


def _verify_findings(report: AuditReport) -> list[str]:
    """Finding ids that came from `core/verify.py` rather than the model."""
    return sorted(f.id for f in report.findings if not f.id.startswith(ADJUDICATED_PREFIX))


class _ContractOnlyProvider:
    """Serves the rate-card parse from the committed cache and refuses
    every other request.

    This is the shape of a real outage: the contract was compiled and
    signed off long before this audit ran, and what is unreachable now is
    the adjudicator. Simply passing a NullProvider would fail earlier, at
    contract compilation, and prove something different.
    """

    def __init__(self) -> None:
        self._cached = CachedProvider(NullProvider())

    def generate_structured(self, prompt: str, schema, model_hint: str) -> dict:
        from llm.adjudicator import AdjudicationBatchResponse
        from llm.provider import ProviderUnavailable

        if schema is AdjudicationBatchResponse:
            raise ProviderUnavailable("simulated outage at the adjudication boundary")
        return self._cached.generate_structured(prompt, schema, model_hint)


def _residual_ids_and_first_candidates(prompt: str) -> dict[str, tuple[str, str] | None]:
    """Parse llm/adjudicator.py's own `_render_residual` format back out of
    a prompt: residual_id -> (record type, record id) of the first
    candidate listed for it, or None if it has no candidates at all.

    Not a guess at the shape -- reading the real rendered pool back out is
    what lets a fake provider answer any residual this run actually
    produces, including ones whose contents change as decompose/verify
    logic changes, without a test needing to hardcode a specific run's
    evidence pool contents.
    """
    import re

    out: dict[str, tuple[str, str] | None] = {}
    current: str | None = None
    for line in prompt.splitlines():
        header = re.match(r"Residual (\S+) \(", line)
        if header:
            current = header.group(1)
            out.setdefault(current, None)
            continue
        candidate = re.match(r"  - (\S+) (\S+)$", line)
        if candidate and current is not None and out[current] is None:
            out[current] = (candidate.group(1), candidate.group(2))
    return out


class _AlwaysExplainsProvider:
    """A fake model that, for every residual in a batch, cites that
    residual's own first-listed candidate record with a generic
    `unreconciled_residual` hypothesis.

    Used only to prove "a model that succeeds contributes something a
    model that is absent cannot" -- the actual class/rationale don't
    matter for that comparison, only that a real, in-pool citation is
    accepted. A residual with an empty pool contributes nothing, same as
    a real model would.

    `run_audit` also needs a provider to compile the contract
    (`RateCardParse`), a completely different schema this class knows
    nothing about answering -- those requests are delegated to the
    committed cache, which does have a valid entry for this run's
    unchanged rate card.
    """

    def __init__(self) -> None:
        self._cached = CachedProvider(NullProvider())

    def generate_structured(self, prompt: str, schema, model_hint: str) -> dict:
        from llm.adjudicator import AdjudicationBatchResponse

        if schema is not AdjudicationBatchResponse:
            return self._cached.generate_structured(prompt, schema, model_hint)

        results = []
        for residual_id, candidate in _residual_ids_and_first_candidates(prompt).items():
            if candidate is None:
                continue
            kind, id_ = candidate
            results.append(
                {
                    "residual_id": residual_id,
                    "hypotheses": [
                        {
                            "discrepancy_class": "unreconciled_residual",
                            "cited_evidence": [{"type": kind, "id": id_}],
                            "rationale": "test fixture: citing the first available candidate",
                        }
                    ],
                }
            )
        return {"results": results}


@pytest.fixture(scope="module")
def committed_report() -> AuditReport:
    return run_audit(COMMITTED_RUN, _offline_provider(), merchant_id=MERCHANT)


@pytest.fixture(scope="module")
def answerable_committed_report() -> AuditReport:
    """The same run, audited by a model that actually answers -- unlike
    `committed_report`, which runs fully offline off a disk cache that may
    or may not still hold an entry for whatever prompt this run's current
    evidence pools happen to produce. Used only where a test needs to show
    what having a working model adds, which the offline fixture cannot
    reliably demonstrate on its own."""
    return run_audit(COMMITTED_RUN, _AlwaysExplainsProvider(), merchant_id=MERCHANT)


@pytest.fixture(scope="module")
def degraded_report() -> AuditReport:
    """The same run, audited while the adjudicator is unreachable."""
    return run_audit(COMMITTED_RUN, _ContractOnlyProvider(), merchant_id=MERCHANT)


@pytest.fixture(scope="module")
def clean_run_dir(tmp_path_factory) -> Path:
    """A zero-discrepancy run materialised to disk, so run_audit sees the
    same four input files a real ingest would hand it."""
    config = GenerationConfig(month="2026-07")
    rate_card = default_rate_card(config.month)
    true_world = build_true_world(config, rate_card, Random(42))
    profile = load_profile("clean")
    reported_world, discrepancies, _flags = apply_discrepancies(true_world, rate_card, profile, Random(43))
    assert discrepancies == [], "the clean profile must plant nothing -- sanity check on the fixture itself"

    out_dir = tmp_path_factory.mktemp("clean") / "clean-seed42"
    write_run(
        reported_world,
        rate_card,
        manifest={"run_id": "clean-seed42", "seed": 42, "profile": "clean"},
        out_dir=out_dir,
        merchant_id=config.merchant_id,
    )
    return out_dir


# ---------------------------------------------------------------------------
# Invariant 3 -- money conservation, over a whole run
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
def test_the_reports_own_totals_reconstruct_both_halves_of_the_conservation_identity(committed_report):
    """`total_unexplained_paise` alone is not invariant 3's whole promise:
    it only sums residual on credits that exist. A settlement batch whose
    credit never arrived contributes no proof, so no residual. The report
    must carry both numbers and must not conflate them."""
    report = committed_report
    ledger = load_ledger(COMMITTED_RUN)
    assert report.total_unexplained_paise == total_unexplained_paise(report.conservation)
    assert report.unclaimed_paise == unclaimed_paise(ledger, report.proofs)
    assert report.total_unaccounted_paise == report.total_unexplained_paise + report.unclaimed_paise


@pytest.mark.timeout(300)
def test_every_conservation_report_reconstructs_its_own_credit_exactly(committed_report):
    for conservation in committed_report.conservation:
        reconstructed = (
            conservation.settled_gross_paise
            - conservation.refunds_paise
            - conservation.fees_paise
            - conservation.tax_paise
            - conservation.chargebacks_paise
            - conservation.adjustments_paise
            + conservation.reversals_paise
            + conservation.unexplained_paise
        )
        assert reconstructed == conservation.credit_paise, conservation.credit_ref.id


@pytest.mark.timeout(300)
def test_the_clean_profile_audits_to_exactly_zero_unaccounted_rupees(clean_run_dir):
    report = run_audit(clean_run_dir, _offline_provider(), merchant_id=MERCHANT)
    assert report.total_unexplained_paise == 0
    assert report.unclaimed_paise == 0
    assert report.total_unaccounted_paise == 0
    assert report.findings == []


# ---------------------------------------------------------------------------
# Invariant 4 -- byte-identical reports
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
def test_two_consecutive_audits_over_the_same_inputs_produce_the_same_report_hash(clean_run_dir):
    first = run_audit(clean_run_dir, _offline_provider(), merchant_id=MERCHANT)
    second = run_audit(clean_run_dir, _offline_provider(), merchant_id=MERCHANT)
    assert first.report_hash == second.report_hash
    assert len(first.report_hash) == 64


@pytest.mark.timeout(300)
def test_the_report_hash_ignores_wall_clock_telemetry(committed_report):
    """A proof's `elapsed_ns` is an observation about this machine on this
    day. Two runs on machines of different speeds must still agree."""
    perturbed = committed_report.model_copy(
        update={
            "proofs": [p.model_copy(update={"elapsed_ns": p.elapsed_ns + 12_345}) for p in committed_report.proofs],
            "wall_clock_ns": committed_report.wall_clock_ns + 999,
            "started_at": "1999-01-01T00:00:00+05:30",
        }
    )
    assert perturbed.compute_report_hash() == committed_report.report_hash


@pytest.mark.timeout(300)
def test_changing_an_input_file_changes_both_the_run_id_and_the_report_hash(clean_run_dir, tmp_path):
    baseline = run_audit(clean_run_dir, _offline_provider(), merchant_id=MERCHANT)

    altered = tmp_path / "altered"
    altered.mkdir()
    for name in ("ledger.json", "settlement_report.json", "bank_statement.json", "rate_card.md", "manifest.json"):
        (altered / name).write_text((clean_run_dir / name).read_text(encoding="utf-8"), encoding="utf-8")

    statement = json.loads((altered / "bank_statement.json").read_text(encoding="utf-8"))
    statement["bank_credits"][0]["amount"]["paise"] += 100
    (altered / "bank_statement.json").write_text(json.dumps(statement, indent=2), encoding="utf-8")

    changed = run_audit(altered, _offline_provider(), merchant_id=MERCHANT)
    assert changed.audit_run_id != baseline.audit_run_id
    assert changed.report_hash != baseline.report_hash


@pytest.mark.timeout(300)
def test_the_audit_run_id_is_a_pure_function_of_the_inputs(clean_run_dir):
    first = run_audit(clean_run_dir, _offline_provider(), merchant_id=MERCHANT)
    second = run_audit(clean_run_dir, _offline_provider(), merchant_id=MERCHANT)
    assert first.audit_run_id == second.audit_run_id
    assert first.input_hash == second.input_hash
    assert set(first.input_hashes) == {
        "ledger.json",
        "settlement_report.json",
        "bank_statement.json",
        "rate_card.md",
    }


# ---------------------------------------------------------------------------
# Invariant 7 -- append-only, idempotent
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
def test_rerunning_an_audit_over_its_own_prior_output_posts_no_new_journal_entries(committed_report):
    from core.ledger import journal_entries_for_auto_findings

    second_pass = journal_entries_for_auto_findings(
        committed_report.findings,
        input_hash=committed_report.input_hash,
        already_posted=committed_report.journal_entries,
        posted_at=committed_report.posted_at,
    )
    assert second_pass == []


@pytest.mark.timeout(300)
def test_only_auto_lane_findings_ever_post(committed_report):
    posted_finding_ids = {entry.id.removeprefix("JNL-") for entry in committed_report.journal_entries}
    auto_finding_ids = {f.id for f in committed_report.findings if f.lane is Lane.AUTO}
    assert posted_finding_ids == auto_finding_ids


# ---------------------------------------------------------------------------
# Degradation -- an absent model never stops the arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
def test_an_unavailable_model_still_produces_a_full_deterministic_report(degraded_report, answerable_committed_report):
    """Chaos scenario 9's promise, at the run level: the adjudicator is the
    only part of an audit a model touches, and losing it costs hypotheses
    about residuals -- never findings, never the conservation identity.

    Compared against the same run with a model that actually answers, so
    what the outage costs is visible rather than asserted in the abstract.
    `committed_report` (offline, cache-only) is deliberately NOT used for
    this comparison: after core/verify.py grew its own refund check and
    llm/adjudicator.py stopped offering a proof's own already-claimed terms
    as evidence for its own residual, the committed disk cache may hold no
    entry at all for this run's current (correctly narrower) evidence
    pools -- which would make BOTH sides of this comparison equally
    "unavailable" for reasons having nothing to do with the outage being
    tested."""
    answerable_report = answerable_committed_report
    assert degraded_report.adjudication_degraded is True
    assert degraded_report.adjudication_degraded_kind == "provider_unavailable"
    assert degraded_report.adjudication is None

    assert degraded_report.findings != []
    assert degraded_report.total_unexplained_paise == answerable_report.total_unexplained_paise
    assert _verify_findings(degraded_report) == _verify_findings(answerable_report), (
        "losing the model must cost hypotheses about residuals and nothing else"
    )
    assert len(degraded_report.findings) < len(answerable_report.findings)


@pytest.mark.timeout(300)
def test_adjudication_can_be_skipped_outright_without_being_reported_as_degraded(clean_run_dir):
    """Not asking is different from asking and being refused, and the
    report must not conflate them -- EVIDENCE.md reports a degradation
    count."""
    skipped = run_audit(clean_run_dir, _offline_provider(), merchant_id=MERCHANT, adjudicate=False)
    assert skipped.adjudication is None
    assert skipped.adjudication_degraded is False
    assert skipped.adjudication_degraded_kind is None
    assert skipped.adjudication_degraded_reason is None


@pytest.mark.timeout(300)
def test_an_audit_with_neither_a_provider_nor_a_contract_refuses_rather_than_guessing(clean_run_dir):
    with pytest.raises(ValueError, match="nothing to recompute"):
        run_audit(clean_run_dir, provider=None, merchant_id=MERCHANT)


# ---------------------------------------------------------------------------
# The committed realistic run -- cross-checked against DECISIONS.md's own
# recorded numbers for this exact dataset (2026-08-26 00:21 entry).
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
def test_the_committed_run_matches_the_numbers_decisions_md_records_for_it(committed_report):
    from core.decompose import DecompositionOutcome, DecompositionTier

    report = committed_report
    tiers = [p.tier for p in report.proofs]
    assert tiers.count(DecompositionTier.STRUCTURAL) == 25
    assert tiers.count(DecompositionTier.SUBSET_SUM) == 6
    assert sum(1 for p in report.proofs if p.outcome is DecompositionOutcome.RESOLVED) == 31
    assert report.total_unexplained_paise == -101_501
    # 49 is the deterministic-engine finding count on this dataset as of
    # the 2026-08-27 session that added core/verify.py's refund check: one
    # more than the 48 DECISIONS.md's 2026-08-26 00:21 entry recorded,
    # because this real dataset genuinely contains a REFUND_AMOUNT_MISMATCH
    # residual that check now catches (see that session's DECISIONS.md
    # entry for the reasoning and the specific finding). Adjudicated
    # findings are excluded here on purpose: they depend on a cached model
    # response, and pinning their count would make an engine-correctness
    # assertion hostage to the cache's contents.
    assert len(_verify_findings(report)) == 49
    # auto_min_calibrated_bps is null in the committed artifact, so nothing
    # is certifiable for AUTO and nothing may post.
    assert report.journal_entries == []


@pytest.mark.timeout(300)
def test_clusters_are_ranked_by_money_and_price_every_finding(committed_report):
    clusters = committed_report.clusters
    assert clusters
    impacts = [c.total_impact.paise for c in clusters]
    assert impacts == sorted(impacts, reverse=True)
    clustered_ids = {fid for c in clusters for fid in c.finding_ids}
    assert clustered_ids == {f.id for f in committed_report.findings}


@pytest.mark.timeout(300)
def test_every_cluster_gets_a_dispute_packet(committed_report):
    assert {p.cluster_id for p in committed_report.dispute_packets} == {
        c.cluster_id for c in committed_report.clusters
    }


@pytest.mark.timeout(300)
def test_a_degradation_names_its_kind_from_a_fixed_vocabulary(degraded_report):
    """`adjudication_degraded_kind` exists so eval/ can count schema
    rejections without pattern-matching an exception message. An absent
    provider and a rejected response are different events and must not be
    conflated by a substring."""
    assert degraded_report.adjudication_degraded_kind == "provider_unavailable"
    assert degraded_report.adjudication_degraded_reason.startswith("provider unavailable")


@pytest.mark.timeout(300)
def test_a_schema_invalid_response_degrades_as_schema_rejected_not_as_unavailable(clean_run_dir, monkeypatch):
    import cli.audit
    from llm.adjudicator import AdjudicationRejected

    def _reject(*args, **kwargs):
        raise AdjudicationRejected("the model's batch response was rejected")

    monkeypatch.setattr(cli.audit, "adjudicate_residuals", _reject)
    monkeypatch.setattr(
        cli.audit,
        "residuals_from_run",
        lambda *a, **k: type("B", (), {"cases": ["x"], "skipped_no_evidence": []})(),
    )

    report = run_audit(clean_run_dir, _offline_provider(), merchant_id=MERCHANT)

    assert report.adjudication_degraded is True
    assert report.adjudication_degraded_kind == "schema_rejected"
    assert report.findings == [], "a clean run still has no findings; the degradation adds none"
