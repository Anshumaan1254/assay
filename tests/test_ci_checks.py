"""Tests for scripts/ci_checks.py -- the assertions CI makes.

These gates only earn their place if they fail when they should. A
`reconciled` check that passes on a report with rupees missing, or a size
check that passes on a five-gigabyte image, is worse than no gate: it is a
green badge over an unverified claim. So each one is tested in both
directions.
"""

from __future__ import annotations

import json
from pathlib import Path

import ci_checks
import pytest


def write_report(tmp_path: Path, **fields) -> Path:
    report = {
        "audit_run_id": "AUD-000000000000",
        "report_hash": "f" * 64,
        "total_unexplained_paise": 0,
        "unclaimed_paise": 0,
        "total_unaccounted_paise": 0,
    }
    report.update(fields)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# reconciled


def test_a_fully_reconciled_report_passes(tmp_path, capsys):
    report = write_report(tmp_path)

    assert ci_checks.main(["reconciled", str(report)]) == 0
    assert "reconciles to exactly zero" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"total_unaccounted_paise": 1}, "one paise unaccounted"),
        ({"total_unaccounted_paise": -1}, "one paise the other way"),
        ({"total_unaccounted_paise": 349636}, "the realistic profile's real figure"),
    ],
)
def test_any_nonzero_residue_fails(tmp_path, fields, why):
    """Not 'within tolerance'. Invariant 3 admits no epsilon."""
    assert ci_checks.main(["reconciled", str(write_report(tmp_path, **fields))]) == 1, why


def test_unclaimed_money_alone_still_fails(tmp_path):
    """The reason this gate reads total_unaccounted_paise and not
    total_unexplained_paise: a whole lost settlement batch produces no bank
    credit, so no credit carries a residual, and the narrower field reports
    a clean audit while rupees are missing. core/conserve.py warns about
    exactly this."""
    report = write_report(
        tmp_path,
        total_unexplained_paise=0,
        unclaimed_paise=451137,
        total_unaccounted_paise=451137,
    )

    assert ci_checks.main(["reconciled", str(report)]) == 1


def test_a_missing_report_is_a_refusal_not_a_pass(tmp_path):
    with pytest.raises(SystemExit):
        ci_checks.main(["reconciled", str(tmp_path / "absent.json")])


# ---------------------------------------------------------------------------
# field


def test_field_prints_one_value(tmp_path, capsys):
    report = write_report(tmp_path, report_hash="a" * 64)

    assert ci_checks.main(["field", str(report), "report_hash"]) == 0
    assert capsys.readouterr().out.strip() == "a" * 64


def test_an_absent_field_is_a_refusal(tmp_path):
    """Otherwise the determinism job would compare two empty strings and
    call it a match."""
    with pytest.raises(SystemExit):
        ci_checks.main(["field", str(write_report(tmp_path)), "no_such_field"])


# ---------------------------------------------------------------------------
# image-size


@pytest.mark.parametrize(
    ("history", "expected_mb"),
    [
        (["0B", "4.1kB", "7.93MB", "361MB"], 361 + 7.93 + 4.1 / 1024),
        (["1.5GB"], 1536.0),
        (["0B", "0B"], 0.0),
        ([""], 0.0),
    ],
)
def test_size_parsing_handles_every_unit_docker_prints(history, expected_mb):
    """`docker history` prints mixed units, and a unit this misparsed would
    silently under-count the image and pass a bloated one."""
    total = 0.0
    for line in history:
        match = ci_checks.SIZE.match(line)
        if match:
            total += float(match.group(1)) * ci_checks.UNIT_MB[match.group(2)]
    assert total == pytest.approx(expected_mb, rel=1e-6)


def test_an_image_over_budget_fails(monkeypatch):
    monkeypatch.setattr(ci_checks, "image_size_mb", lambda tag: 640.0)

    assert ci_checks.main(["image-size", "assay", "--budget-mb", "500"]) == 1


def test_an_image_under_budget_passes(monkeypatch, capsys):
    monkeypatch.setattr(ci_checks, "image_size_mb", lambda tag: 464.2)

    assert ci_checks.main(["image-size", "assay", "--budget-mb", "500"]) == 0
    assert "headroom" in capsys.readouterr().out


def test_the_budget_boundary_is_not_off_by_one(monkeypatch):
    """Exactly at budget passes; a hair over does not."""
    monkeypatch.setattr(ci_checks, "image_size_mb", lambda tag: 500.0)
    assert ci_checks.main(["image-size", "assay", "--budget-mb", "500"]) == 0

    monkeypatch.setattr(ci_checks, "image_size_mb", lambda tag: 500.01)
    assert ci_checks.main(["image-size", "assay", "--budget-mb", "500"]) == 1
