"""C09 -- LLM API entirely unavailable.

The most important scenario in the suite (CLAUDE.md's own words): swap in
llm/providers/null.py and the deterministic engine still completes the
full audit, reporting LLM-dependent items as exceptions rather than
guessing or crashing. THE AUDIT STILL PRODUCES A NUMBER.

This is a thin chaos/ wrapper around the already-proven, already-tested
behaviour in tests/test_cli_audit.py -- cli/audit.py::_adjudicate() catches
ProviderUnavailable and records AuditReport.adjudication_degraded* rather
than propagating. What's added here is the structured incident log this
package exists for, and driving it through resume_or_run() (the real
`assay audit` code path) rather than the bare run_audit() function.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chaos.incident import chaos_scenario
from llm.contract_parser import compile_rate_card
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider
from store.resumable import resume_or_run

MERCHANT = "MERCH-0001"
REPO_ROOT = Path(__file__).resolve().parent.parent
# The committed fixture, not datagen -- chaos/ is under the same invariant
# 5 quarantine as core/, llm/, cli/: it must never import datagen.
REALISTIC_RUN_DIR = REPO_ROOT / "runs" / "realistic-seed42"


@pytest.mark.timeout(180)
def test_c09_llm_entirely_unavailable_still_produces_a_full_report(tmp_path):
    with chaos_scenario(
        "C09",
        title="LLM API entirely unavailable",
        category="degraded_gracefully",
        failure_injected="every generate_structured() call raises ProviderUnavailable (NullProvider)",
        expected_behavior=(
            "the deterministic engine completes the full audit; LLM-dependent residuals stay "
            "unexplained exceptions rather than guesses; the report is honest about the degradation"
        ),
    ) as scenario:
        # Two different LLM boundaries, and only one of them is under test.
        # The contract compile is served from the committed cache, exactly
        # as before -- see tests/test_cli_audit.py's own precedent for "the
        # model that matters here is the adjudicator, not the contract
        # compiler."
        contract = compile_rate_card(
            (REALISTIC_RUN_DIR / "rate_card.md").read_text(encoding="utf-8"),
            CachedProvider(NullProvider()),
        )

        # The audit itself gets a BARE NullProvider, not a cached one. This
        # scenario used to pass CachedProvider(NullProvider()) here, and a
        # later-committed cache entry for this run's adjudication prompt
        # then answered the very call the scenario exists to watch fail --
        # NullProvider was never reached and adjudication_degraded stayed
        # False. A cache hit cannot disarm an injection it never sees, so
        # the injected provider is now unwrapped: the assertions below can
        # only hold if ProviderUnavailable actually propagated from it.
        report = resume_or_run(
            REALISTIC_RUN_DIR,
            NullProvider(),
            merchant_id=MERCHANT,
            contract=contract,
            store_path=tmp_path / "store.db",
        )

        assert report.adjudication_degraded is True
        assert report.adjudication_degraded_kind == "provider_unavailable"
        assert report.report_hash, "the audit still produces a number"
        assert report.findings != [] or report.total_unaccounted_paise >= 0
        # The deterministic half of the engine (decompose/verify/conserve)
        # is entirely unaffected by an absent model.
        assert report.proofs != []
        assert report.total_unexplained_paise == sum(c.unexplained_paise for c in report.conservation)

        scenario.note(
            f"adjudication_degraded_reason={report.adjudication_degraded_reason!r}; "
            f"total_unaccounted_paise={report.total_unaccounted_paise}"
        )
        scenario.money_impact_paise = report.total_unaccounted_paise
