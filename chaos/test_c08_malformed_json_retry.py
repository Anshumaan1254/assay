"""C08 -- LLM returns malformed JSON, then valid on retry.

llm/providers/retrying.py::RetryingProvider retries only MalformedResponse
(an empty/non-JSON/non-object body) with bounded backoff, then hard-fails
to ProviderUnavailable -- unit-tested in tests/test_retrying_provider.py.
This drives the same fault through a real audit run: a flaky adjudication
call that fails twice and then answers correctly must be invisible to the
final report (the retry succeeds before the batch is ever seen as failed),
and a persistently-flaky one must degrade the run cleanly rather than
retry forever or crash.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chaos.incident import chaos_scenario
from llm.adjudicator import AdjudicationBatchResponse
from llm.provider import MalformedResponse
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider
from llm.providers.retrying import RetryConfig, RetryingProvider
from store.resumable import resume_or_run

MERCHANT = "MERCH-0001"
REPO_ROOT = Path(__file__).resolve().parent.parent
# The committed fixture, not datagen -- chaos/ is under the same invariant
# 5 quarantine as core/, llm/, cli/: it must never import datagen.
REALISTIC_RUN_DIR = REPO_ROOT / "runs" / "realistic-seed42"


def _first_candidates(prompt: str) -> dict[str, tuple[str, str] | None]:
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


class _FlakyAdjudicationInner:
    """Malformed on the first `fail_times` adjudication calls, then answers
    for real by citing each residual's own first-listed candidate.
    Contract-parse calls are always delegated straight to the cache."""

    def __init__(self, fail_times: int) -> None:
        self._cached = CachedProvider(NullProvider())
        self._fail_times = fail_times
        self.attempts = 0

    def generate_structured(self, prompt: str, schema, model_hint: str) -> dict:
        if schema is not AdjudicationBatchResponse:
            return self._cached.generate_structured(prompt, schema, model_hint)

        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise MalformedResponse(f"attempt {self.attempts}: simulated glitch")

        results = []
        for residual_id, candidate in _first_candidates(prompt).items():
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


@pytest.mark.timeout(180)
def test_c08_two_malformed_responses_then_a_valid_one_is_invisible_to_the_report(tmp_path):
    with chaos_scenario(
        "C08",
        title="LLM returns malformed JSON, then valid on retry",
        category="degraded_gracefully",
        failure_injected="the adjudication call returns a malformed response twice, then a valid one",
        expected_behavior="RetryingProvider absorbs it transparently; the final report is unaffected",
    ) as scenario:
        inner = _FlakyAdjudicationInner(fail_times=2)
        provider = RetryingProvider(inner, config=RetryConfig(max_retries=5, backoff_base_seconds=0.0))

        report = resume_or_run(
            REALISTIC_RUN_DIR, provider, merchant_id=MERCHANT, store_path=tmp_path / "store.db"
        )

        assert report.report_hash
        assert report.adjudication_degraded is False, "a retry that succeeds must not be reported as a degradation"
        assert inner.attempts >= 3, "must have actually retried past the two glitches"

        scenario.note(f"adjudication call attempts (incl. retries) = {inner.attempts}")


@pytest.mark.timeout(180)
def test_c08_a_persistently_malformed_response_degrades_cleanly_never_retries_forever(tmp_path):
    with chaos_scenario(
        "C08B",
        title="LLM returns malformed JSON forever",
        category="degraded_gracefully",
        failure_injected="every adjudication call returns a malformed response, without exception",
        expected_behavior="a bounded number of retries, then a clean degradation -- never an infinite retry loop",
    ) as scenario:
        inner = _FlakyAdjudicationInner(fail_times=10_000)
        max_retries = 3
        provider = RetryingProvider(inner, config=RetryConfig(max_retries=max_retries, backoff_base_seconds=0.0))

        report = resume_or_run(
            REALISTIC_RUN_DIR, provider, merchant_id=MERCHANT, store_path=tmp_path / "store.db"
        )

        assert report.report_hash, "the audit still produces a number despite the degradation"
        assert report.adjudication_degraded is True
        assert report.adjudication_degraded_kind == "provider_unavailable"
        assert inner.attempts == max_retries + 1, "exactly one initial attempt plus max_retries -- never more"

        scenario.note(f"gave up after exactly {inner.attempts} attempts (max_retries={max_retries})")
