"""C07 -- LLM returns a fabricated transaction id.

llm/adjudicator.py's reference checker (invariant 6) already rejects a
citation to a record that doesn't exist in the ledger, logs it, and drops
the citation. That's unit-tested in tests/test_adjudicator.py. What this
scenario proves, end to end through a real audit run, is that the REPORT
still completes and the residual stays an honest, unexplained exception --
never a guessed Finding -- rather than only checking the rejection in
isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chaos.incident import chaos_scenario
from llm.adjudicator import AdjudicationBatchResponse
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider
from store.resumable import resume_or_run

MERCHANT = "MERCH-0001"
REPO_ROOT = Path(__file__).resolve().parent.parent
# The committed fixture, not datagen -- chaos/ is under the same invariant
# 5 quarantine as core/, llm/, cli/: it must never import datagen.
REALISTIC_RUN_DIR = REPO_ROOT / "runs" / "realistic-seed42"
FABRICATED_REF = ("payment", "PAY-DOES-NOT-EXIST-99999")


class _FabricatingProvider:
    """Answers the contract parse from the committed cache, and answers
    every adjudication batch by citing a record id that exists nowhere in
    the ledger -- a pure fabrication, not merely evidence outside the pool
    (that's a different, already-distinct rejection reason)."""

    def __init__(self) -> None:
        self._cached = CachedProvider(NullProvider())

    def generate_structured(self, prompt: str, schema, model_hint: str) -> dict:
        if schema is not AdjudicationBatchResponse:
            return self._cached.generate_structured(prompt, schema, model_hint)

        import re

        residual_ids = re.findall(r"Residual (\S+) \(", prompt)
        kind, fabricated_id = FABRICATED_REF
        return {
            "results": [
                {
                    "residual_id": residual_id,
                    "hypotheses": [
                        {
                            "discrepancy_class": "unreconciled_residual",
                            "cited_evidence": [{"type": kind, "id": fabricated_id}],
                            "rationale": "test fixture: a fabricated citation",
                        }
                    ],
                }
                for residual_id in residual_ids
            ]
        }


@pytest.mark.timeout(180)
def test_c07_a_fabricated_citation_is_rejected_and_the_audit_still_completes(tmp_path):
    with chaos_scenario(
        "C07",
        title="LLM returns a fabricated transaction id",
        category="degraded_gracefully",
        failure_injected=f"every adjudication hypothesis cites {FABRICATED_REF[1]!r}, which is not in the ledger",
        expected_behavior=(
            "the citation is rejected and logged, the residual stays an unexplained exception "
            "(never a guessed Finding), and the full audit report still completes"
        ),
    ) as scenario:
        report = resume_or_run(
            REALISTIC_RUN_DIR, _FabricatingProvider(), merchant_id=MERCHANT, store_path=tmp_path / "store.db"
        )

        assert report.report_hash, "the audit still produces a number"
        assert report.adjudication is not None
        assert report.adjudication.citations_rejected_nonexistent > 0
        assert all(fabricated_id_not_cited(f) for f in report.findings)

        rejected_results = [r for r in report.adjudication.results if r.rejected_count > 0]
        assert rejected_results, "at least one residual's hypothesis must have been rejected"
        assert all(r.accepted_hypotheses == [] for r in rejected_results), (
            "a fully-rejected hypothesis must produce no Finding -- the residual stays an exception"
        )

        scenario.note(
            f"citations_rejected_nonexistent={report.adjudication.citations_rejected_nonexistent}, "
            f"rejected_results={len(rejected_results)}"
        )


def fabricated_id_not_cited(finding) -> bool:
    return not any(ref.id == FABRICATED_REF[1] for ref in finding.evidence_ids)
