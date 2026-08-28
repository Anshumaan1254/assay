"""Tests for `assay evidence` -- cli/evidence.py."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

import cli
from eval.evidence import wrap

runner = CliRunner()


def test_assay_evidence_prints_a_valid_document_and_confirms_the_hash(tmp_path, monkeypatch):
    target = tmp_path / "EVIDENCE.md"
    target.write_text(wrap("# some real evidence\n\nnumbers go here\n"), encoding="utf-8")
    monkeypatch.setattr("eval.evidence.EVIDENCE_PATH", target)

    result = runner.invoke(cli.app, ["evidence"])

    assert result.exit_code == 0, result.output
    assert "some real evidence" in result.output
    assert "OK: matches its declared body hash" in result.output


def test_assay_evidence_refuses_cleanly_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("eval.evidence.EVIDENCE_PATH", tmp_path / "does-not-exist.md")

    result = runner.invoke(cli.app, ["evidence"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "refused" in result.output


def test_assay_evidence_refuses_cleanly_when_hand_edited(tmp_path, monkeypatch):
    target = tmp_path / "EVIDENCE.md"
    document = wrap("# some real evidence\n\nnumbers go here\n")
    tampered = document.replace("numbers go here", "numbers go here, but improved")
    target.write_text(tampered, encoding="utf-8")
    monkeypatch.setattr("eval.evidence.EVIDENCE_PATH", target)

    result = runner.invoke(cli.app, ["evidence"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "refused" in result.output
    assert "edited by hand" in result.output


def test_assay_evidence_prints_non_ascii_content_through_a_real_process_on_a_legacy_codepage():
    """Regression guard: `typer.echo(document)` crashed with
    UnicodeEncodeError on Windows' default cp1252 console codepage, since
    EVIDENCE.md's committed prose contains a rupee sign -- caught by
    manually running `assay evidence` for real, not by any CliRunner test,
    since CliRunner invokes cli.app directly and never goes through
    main()/`_use_utf8_streams()`. Forces PYTHONIOENCODING to a legacy,
    non-UTF-8 codec on a real subprocess to reproduce the exact failure
    mode, rather than relying on this machine's actual console codepage.
    """
    result = subprocess.run(
        [sys.executable, "-m", "cli", "evidence"],
        cwd=str(Path(__file__).resolve().parent.parent),
        env={**os.environ, "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        text=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert b"UnicodeEncodeError" not in result.stderr


def test_assay_evidence_refuses_cleanly_when_the_banner_is_missing(tmp_path, monkeypatch):
    target = tmp_path / "EVIDENCE.md"
    target.write_text("# not a generated document at all\n", encoding="utf-8")
    monkeypatch.setattr("eval.evidence.EVIDENCE_PATH", target)

    result = runner.invoke(cli.app, ["evidence"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "refused" in result.output
