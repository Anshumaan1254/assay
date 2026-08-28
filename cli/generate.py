"""`assay generate` -- a subprocess wrapper over `python -m datagen.cli`,
never a Python-level import of datagen/.

Invariant 5 quarantines ground truth: nothing under cli/ may contain a
literal `import datagen` / `from datagen import ...` anywhere in the file
(tests/test_architecture.py AST-walks the whole file, not just module
scope, so even a lazy import inside a function body would trip it) --  and
unlike `assay eval`, there is no eval/-side wrapper to lazily import
instead, since generation logic lives directly in datagen/. Shelling out
keeps the guarantee literally: a string handed to subprocess is not an AST
Import node, and no Python import of datagen ever happens inside the assay
process.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from cli.procutil import run_and_stream


def generate(
    profile: str = typer.Option(
        "realistic", "--profile", help="clean | realistic | stress, or a path to a custom YAML"
    ),
    seed: int = typer.Option(..., "--seed", help="RNG seed -- required, determinism depends on it"),
    silent_corruption: bool = typer.Option(False, "--silent-corruption"),
    month: str = typer.Option("2026-07", "--month", help="YYYY-MM"),
    out: Path = typer.Option(Path("runs"), "--out", help="Parent dir; writes out/<run_id>/"),  # noqa: B008
    truth_out: Path = typer.Option(  # noqa: B008 -- Typer's own documented pattern
        Path("truth/ground_truth.json"), "--truth-out"
    ),
    run_id: str = typer.Option(None, "--run-id", help="Override the derived run_id"),
) -> None:
    """Generate one settlement month with planted discrepancies, via
    `python -m datagen.cli` -- see that command's own docstring for what it
    produces. Runs as a separate process under the same interpreter
    (`sys.executable`) this `assay` process is running under."""
    cmd = [
        sys.executable,
        "-m",
        "datagen.cli",
        # No "generate" subcommand name: datagen/cli.py's Typer app has a
        # single command and no callback, so Typer collapses it to a bare
        # invocation -- the same behaviour DECISIONS.md already accepted
        # for this exact module (see cli/__init__.py's own callback
        # docstring for the general rule).
        "--profile",
        profile,
        "--seed",
        str(seed),
        "--month",
        month,
        "--out",
        str(out),
        "--truth-out",
        str(truth_out),
    ]
    if silent_corruption:
        cmd.append("--silent-corruption")
    if run_id is not None:
        cmd.extend(["--run-id", run_id])

    code = run_and_stream(cmd)
    if code:
        raise typer.Exit(code=code)
