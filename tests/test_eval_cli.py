"""Tests for eval/cli.py::run_eval.

Runs a real, tiny sweep (one profile, one seed, no ablation) off the
committed .llm_cache/ -- offline, no GEMINI_API_KEY needed. Deliberately
does NOT test the write_diagrams=True (default) path against the real
docs/reliability.{svg,png}: that would dirty the actual committed diagram
on every local test run. write_svg/write_png are spied on via
eval.diagram, not via redirecting their path (their default `path=` is
bound at function-definition time, so monkeypatching eval.diagram.SVG_PATH
after the fact would not change it).
"""

from __future__ import annotations

import pytest

from eval.cli import run_eval


@pytest.mark.timeout(120)
def test_write_diagrams_false_never_touches_docs_reliability(tmp_path, monkeypatch):
    import eval.diagram

    def _boom(*args, **kwargs):
        raise AssertionError("write_svg/write_png must not be called when write_diagrams=False")

    monkeypatch.setattr(eval.diagram, "write_svg", _boom)
    monkeypatch.setattr(eval.diagram, "write_png", _boom)

    evidence_path = run_eval(
        profiles=["clean"],
        seeds=[42],
        out=tmp_path / "results",
        write_evidence=False,
        ablate=False,
        write_diagrams=False,
        echo=lambda *a, **k: None,
    )

    assert evidence_path.is_file()
