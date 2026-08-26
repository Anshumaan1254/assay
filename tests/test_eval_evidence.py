"""Tests for the EVIDENCE.md generator and its hand-edit guard.

The guard is the point. EVIDENCE.md is a generated accuracy report, and a
generated accuracy report is only worth reading if nobody can quietly
improve a number in it. So the tests that matter here are the ones that
prove an edit is *detected*, not the ones that prove the happy path
renders.

`scripts/check_evidence.py` duplicates the banner format rather than
importing it from `eval/evidence.py`, so that it runs in a bare CI checkout
with nothing installed. That duplication is a drift risk, so it gets its
own test.
"""

from __future__ import annotations

from pathlib import Path

import check_evidence
import pytest

from eval.evidence import BANNER_END, body_hash, split, wrap

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# The banner
# ---------------------------------------------------------------------------


def test_wrapping_then_splitting_round_trips_the_body():
    body = "# EVIDENCE\n\nsome numbers\n"
    declared, recovered = split(wrap(body))
    assert recovered == body
    assert declared == body_hash(body)


def test_a_generated_file_passes_the_guard(tmp_path):
    path = tmp_path / "EVIDENCE.md"
    path.write_text(wrap("# EVIDENCE\n\n42 rupees\n"), encoding="utf-8")

    ok, message = check_evidence.check(path)

    assert ok, message


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda text: text.replace("42 rupees", "43 rupees"), id="a-number-changed"),
        pytest.param(lambda text: text + "\n", id="one-trailing-newline"),
        pytest.param(lambda text: text.replace("EVIDENCE", "EVIDENCE "), id="one-space"),
    ],
)
def test_any_hand_edit_fails_the_guard(tmp_path, edit):
    path = tmp_path / "EVIDENCE.md"
    path.write_text(edit(wrap("# EVIDENCE\n\n42 rupees\n")), encoding="utf-8")

    ok, message = check_evidence.check(path)

    assert not ok
    assert "edited by hand" in message


def test_a_file_with_no_banner_is_rejected_rather_than_waved_through(tmp_path):
    path = tmp_path / "EVIDENCE.md"
    path.write_text("# EVIDENCE\n\nhand-written\n", encoding="utf-8")

    ok, message = check_evidence.check(path)

    assert not ok
    assert "no generated-file banner" in message


def test_a_missing_file_is_reported_as_missing_not_as_passing(tmp_path):
    ok, message = check_evidence.check(tmp_path / "nope.md")
    assert not ok
    assert "does not exist" in message


def test_the_standalone_guard_agrees_with_the_generator_about_the_format():
    """scripts/check_evidence.py cannot import eval/, so it restates the
    banner constants. If the two ever disagree, the guard silently stops
    guarding."""
    assert check_evidence.BANNER_END == BANNER_END
    assert check_evidence.BANNER_START in wrap("body")
    assert check_evidence.HASH_KEY in wrap("body")


def test_the_guard_script_exits_nonzero_on_a_tampered_file(tmp_path, capsys):
    path = tmp_path / "EVIDENCE.md"
    path.write_text(wrap("body\n").replace("body", "tampered"), encoding="utf-8")

    code = check_evidence.main(["check_evidence.py", str(path)])

    assert code == 1
    assert "FAIL" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# End-to-end: a real (small) sweep renders a real document
# ---------------------------------------------------------------------------


@pytest.mark.timeout(900)
def test_a_real_sweep_renders_every_section_in_order_and_passes_its_own_guard(tmp_path):
    """One clean-profile run, rendered end to end. Slow, and worth it: it
    is the only test that proves the fourteen sections actually assemble
    from real measured numbers rather than from a hand-built fixture that
    could drift from what the harness emits."""
    from eval.evidence import render
    from eval.sweep import run_sweep
    from llm.providers.cached import CachedProvider
    from llm.providers.null import NullProvider

    report = run_sweep(
        lambda: CachedProvider(NullProvider()),
        profiles=["clean"],
        seeds=[15],
        ablate=False,
    )
    document = render(report)

    headings = [line for line in document.splitlines() if line.startswith("## ")]
    assert [h.split(".")[0] for h in headings] == [f"## {n}" for n in range(1, 15)]

    path = tmp_path / "EVIDENCE.md"
    path.write_text(document, encoding="utf-8")
    ok, message = check_evidence.check(path)
    assert ok, message

    # The clean profile plants nothing, so it must reconcile exactly.
    assert report.clean_profile_unaccounted_paise == 0
    assert "reconciles to exactly zero" in document
    assert report.determinism.matches
