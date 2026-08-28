"""Tests for `assay replay` -- cli/report.py::replay_run. Money-critical
(invariant 4, proved on demand), written test-first per the working
agreement.
"""

from __future__ import annotations

import json
from pathlib import Path
from random import Random

import pytest
from typer.testing import CliRunner

import cli
from datagen.config import GenerationConfig, load_profile
from datagen.inject import apply_discrepancies
from datagen.ratecard import default_rate_card
from datagen.world import build_true_world
from datagen.writer import write_run
from llm.providers.cached import CachedProvider
from llm.providers.null import NullProvider

runner = CliRunner()


def _offline_provider() -> CachedProvider:
    return CachedProvider(NullProvider())


@pytest.fixture(scope="module")
def clean_run_dir(tmp_path_factory) -> Path:
    config = GenerationConfig(month="2026-07")
    rate_card = default_rate_card(config.month)
    true_world = build_true_world(config, rate_card, Random(42))
    profile = load_profile("clean")
    reported_world, discrepancies, _flags = apply_discrepancies(true_world, rate_card, profile, Random(43))
    assert discrepancies == [], "sanity check on the fixture itself"

    out_dir = tmp_path_factory.mktemp("clean") / "clean-seed42"
    write_run(
        reported_world,
        rate_card,
        manifest={"run_id": "clean-seed42", "seed": 42, "profile": "clean"},
        out_dir=out_dir,
        merchant_id=config.merchant_id,
    )
    return out_dir


@pytest.fixture
def audited_run(clean_run_dir, tmp_path, monkeypatch) -> tuple[str, Path]:
    monkeypatch.setattr(cli, "default_provider", _offline_provider)
    store_path = tmp_path / "store.db"
    monkeypatch.setenv("ASSAY_STORE_PATH", str(store_path))

    result = runner.invoke(cli.app, ["audit", "--run-dir", str(clean_run_dir), "--no-adjudicate"])
    assert result.exit_code == 0, result.output

    run_id = next(
        line.split(":", 1)[1].strip() for line in result.output.splitlines() if line.startswith("audit run:")
    )
    return run_id, store_path


def test_replay_passes_and_matches_the_stored_hash(audited_run, monkeypatch):
    run_id, store_path = audited_run
    monkeypatch.setattr(cli, "default_provider", _offline_provider)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(store_path))

    result = runner.invoke(cli.app, ["replay", run_id])

    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_replay_fails_cleanly_when_the_stored_hash_was_tampered_with(audited_run, clean_run_dir, monkeypatch):
    run_id, store_path = audited_run
    monkeypatch.setattr(cli, "default_provider", _offline_provider)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(store_path))

    report_json_path = clean_run_dir / "report.json"
    payload = json.loads(report_json_path.read_text(encoding="utf-8"))
    payload["report_hash"] = "0" * 64
    report_json_path.write_text(json.dumps(payload), encoding="utf-8")

    result = runner.invoke(cli.app, ["replay", run_id])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "0" * 64 in result.output


def test_replay_resolves_its_provider_through_the_same_patchable_name_as_the_rest_of_the_cli(
    audited_run, monkeypatch
):
    """Regression guard: replay_run() previously resolved its own provider
    via a private `from cli.loaders import default_provider` inside
    cli/report.py, which silently bypassed `monkeypatch.setattr(cli,
    "default_provider", ...)` -- that patches cli/__init__.py's copy of the
    name, not cli.loaders' original. On a machine with no cached response
    for a genuinely new prompt, that private copy could reach a real
    network call instead of the clean refusal every other command gives,
    and this test suite's own stubbing was silently not being exercised
    for replay at all. Proven here by counting calls through the
    patchable name, not just checking the outcome."""
    run_id, store_path = audited_run
    calls = {"n": 0}

    def _counting_provider():
        calls["n"] += 1
        return _offline_provider()

    monkeypatch.setattr(cli, "default_provider", _counting_provider)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(store_path))

    result = runner.invoke(cli.app, ["replay", run_id])

    assert result.exit_code == 0, result.output
    assert calls["n"] >= 1


def test_replay_actually_uses_the_patched_provider_not_a_private_copy(audited_run, monkeypatch):
    """The direct version of the regression above: if replay_run() ever
    reverts to resolving its own provider internally, this stub raising
    would go unused and the command would still exit 0 -- this test would
    then fail to fail, which is why the call-counting test above also
    exists as a second, independent signal."""

    def _boom():
        raise AssertionError("assay replay must use the patched cli.default_provider, not a private copy")

    run_id, store_path = audited_run
    monkeypatch.setattr(cli, "default_provider", _boom)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(store_path))

    result = runner.invoke(cli.app, ["replay", run_id])

    assert result.exit_code != 0
    assert isinstance(result.exception, AssertionError)


def test_replay_refuses_cleanly_when_no_provider_is_available(audited_run, monkeypatch):
    from llm.provider import ProviderUnavailable

    def _unavailable():
        raise ProviderUnavailable("simulated: no GEMINI_API_KEY and no cache entry")

    run_id, store_path = audited_run
    monkeypatch.setattr(cli, "default_provider", _unavailable)
    monkeypatch.setenv("ASSAY_STORE_PATH", str(store_path))

    result = runner.invoke(cli.app, ["replay", run_id])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "refused" in result.output


def test_replay_refuses_cleanly_on_an_unknown_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ASSAY_STORE_PATH", str(tmp_path / "empty.db"))

    result = runner.invoke(cli.app, ["replay", "AUD-does-not-exist"])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "refused" in result.output
