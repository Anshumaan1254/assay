"""Tests for scripts/evidence_diff.py.

The masking is the weak point of this gate. Mask too little and the check
can never pass on a machine that is not the one which generated
EVIDENCE.md; mask too much and it silently swallows the regressions it
exists to catch. So both directions are pinned here: environment rows are
ignored, and every measured number is not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import evidence_diff
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "evidence_diff.py"
EVIDENCE = REPO_ROOT / "EVIDENCE.md"


def run_diff(baseline: Path, current: Path) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: PLW1510 -- the exit code is what these tests assert on
        [sys.executable, str(SCRIPT), "--baseline", str(baseline), "--current", str(current)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture(scope="module")
def evidence_text() -> str:
    if not EVIDENCE.is_file():
        pytest.skip("EVIDENCE.md is not present")
    return EVIDENCE.read_text(encoding="utf-8")


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Environment rows: must be ignored


@pytest.mark.parametrize(
    ("label", "replacement"),
    [
        ("Run timestamp", "| Run timestamp | 2019-01-01T00:00:00.000000+00:00 |"),
        ("Git commit", "| Git commit | `0000000000000000000000000000000000000000` |"),
        ("Python", "| Python | 3.11.99 |"),
        ("Platform", "| Platform | Linux 6.8.0 (x86_64) |"),
        ("Wall clock (whole sweep)", "| Wall clock (whole sweep) | 1.0s |"),
        ("Records/sec", "| Records/sec | **1** |"),
        ("Cache hit rate", "| Cache hit rate | 100.0% (36 hits / 36 calls) |"),
        ("Run 1 report hash", "| Run 1 report hash | `" + "a" * 64 + "` |"),
    ],
)
def test_environment_rows_are_ignored(tmp_path, evidence_text, label, replacement):
    """A Linux CI runner differs from the Windows box that generated the
    committed document in every one of these. If any were compared, the
    gate could never go green anywhere but on one machine."""
    changed = "\n".join(
        replacement if line.startswith(f"| {label} |") else line
        for line in evidence_text.splitlines()
    )
    assert changed != evidence_text, f"no row labelled {label!r} to rewrite"

    result = run_diff(write(tmp_path, "base.md", evidence_text), write(tmp_path, "cur.md", changed))

    assert result.returncode == 0, result.stderr


def test_the_body_hash_banner_is_ignored(tmp_path, evidence_text):
    """It is a hash OF the body, so it necessarily differs whenever any
    masked row does. check_evidence.py is what verifies it."""
    changed = evidence_text.replace(
        evidence_text.split("body-sha256: ")[1][:64], "b" * 64
    )

    result = run_diff(write(tmp_path, "base.md", evidence_text), write(tmp_path, "cur.md", changed))

    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------
# Measured numbers: must be caught


@pytest.mark.parametrize(
    ("old", "new", "what"),
    [
        ("| **all** | **1400** | **0** |", "| **all** | **1400** | **7** |", "false positives"),
        ("**55.2%**", "**41.3%**", "value-weighted recall"),
        ("`rc-bac663f10b17`", "`rc-0000deadbeef`", "contract version"),
        ("| Expected calibration error | **0.0029** |",
         "| Expected calibration error | **0.4200** |", "calibration error"),
        ("| Verdict | **match** |", "| Verdict | **MISMATCH** |", "determinism verdict"),
        ("| Replay matches, live API calls made | 0 |",
         "| Replay matches, live API calls made | 4 |", "network calls during replay"),
    ],
)
def test_measured_numbers_are_caught(tmp_path, evidence_text, old, new, what):
    if old not in evidence_text:
        pytest.skip(f"{what}: anchor {old!r} not in the current EVIDENCE.md")
    changed = evidence_text.replace(old, new, 1)

    result = run_diff(write(tmp_path, "base.md", evidence_text), write(tmp_path, "cur.md", changed))

    assert result.returncode == 1, f"a changed {what} must fail the gate"
    assert "does not reproduce" in result.stderr


def test_the_determinism_verdict_is_not_masked_even_though_the_hashes_are():
    """The hash VALUES vary by platform (line endings, run_dir separator --
    DECISIONS.md 2026-08-30 00:20), but that the two runs agree does not.
    Masking the verdict alongside them would retire invariant 4's check."""
    assert "Run 1 report hash" in evidence_diff.MASKED_LABELS
    assert "Verdict" not in evidence_diff.MASKED_LABELS
    assert "Replay matches, live API calls made" not in evidence_diff.MASKED_LABELS


def test_identical_documents_pass(tmp_path, evidence_text):
    result = run_diff(
        write(tmp_path, "base.md", evidence_text), write(tmp_path, "cur.md", evidence_text)
    )

    assert result.returncode == 0, result.stderr
    assert "matches the committed one" in result.stdout


def test_a_missing_current_document_is_a_refusal_not_a_pass(tmp_path, evidence_text):
    result = run_diff(write(tmp_path, "base.md", evidence_text), tmp_path / "nope.md")

    assert result.returncode != 0
    assert "does not exist" in result.stderr
