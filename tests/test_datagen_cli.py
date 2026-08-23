"""datagen/cli.py smoke tests via Typer's CliRunner. Note the CLI has
exactly one command, which Typer collapses to the app's own callback --
invocation is `datagen.cli --profile ... --seed ...`, no subcommand name."""

from __future__ import annotations

from typer.testing import CliRunner

from datagen.cli import app

runner = CliRunner()


def test_generate_clean_profile_succeeds_and_writes_files(tmp_path):
    out_dir = tmp_path / "runs"
    truth_path = tmp_path / "truth" / "ground_truth.json"
    result = runner.invoke(
        app,
        [
            "--profile", "clean",
            "--seed", "1",
            "--month", "2026-07",
            "--out", str(out_dir),
            "--truth-out", str(truth_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "run_id: clean-seed1" in result.output
    assert "Payments:" in result.output
    assert "Ground truth manifest:" in result.output
    assert (out_dir / "clean-seed1" / "ledger.json").exists()
    assert truth_path.exists()


def test_generate_realistic_profile_reports_nonzero_discrepancies(tmp_path):
    out_dir = tmp_path / "runs"
    truth_path = tmp_path / "truth" / "ground_truth.json"
    result = runner.invoke(
        app,
        [
            "--profile", "realistic",
            "--seed", "42",
            "--out", str(out_dir),
            "--truth-out", str(truth_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert '"D01": 8' in result.output


def test_generate_with_silent_corruption_flag(tmp_path):
    out_dir = tmp_path / "runs"
    truth_path = tmp_path / "truth" / "ground_truth.json"
    result = runner.invoke(
        app,
        [
            "--profile", "clean",
            "--seed", "1",
            "--silent-corruption",
            "--out", str(out_dir),
            "--truth-out", str(truth_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "silent_corruption: True" in result.output
    assert "run_id: clean-seed1-corrupt" in result.output


def test_generate_missing_seed_fails():
    result = runner.invoke(app, ["--profile", "clean"])
    assert result.exit_code != 0


def test_generate_unknown_profile_name_fails():
    result = runner.invoke(app, ["--profile", "does-not-exist", "--seed", "1"])
    assert result.exit_code != 0


def test_generate_run_id_override(tmp_path):
    out_dir = tmp_path / "runs"
    truth_path = tmp_path / "truth" / "ground_truth.json"
    result = runner.invoke(
        app,
        [
            "--profile", "clean",
            "--seed", "1",
            "--run-id", "custom-name",
            "--out", str(out_dir),
            "--truth-out", str(truth_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "custom-name" / "ledger.json").exists()
