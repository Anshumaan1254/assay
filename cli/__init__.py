"""Typer app. The CLI is the product."""

from __future__ import annotations

from pathlib import Path

import typer

from cli.explain import explain_record
from cli.loaders import (
    DEFAULT_RUN_DIR,
    default_provider,
    infer_merchant_id,
    load_bank_credits,
    load_calibration_artifact,
    load_contract,
    load_ledger,
)

app = typer.Typer(help="Assay -- settlement audit engine.")


@app.callback()
def _callback() -> None:
    """Assay -- settlement audit engine.

    A no-op callback: Typer collapses a Typer() app down to its single
    command's own bare invocation (no subcommand name) when exactly one
    @app.command() is registered and no callback exists -- the same
    behaviour DECISIONS.md already accepted for datagen/cli.py. Here the
    literal syntax `assay explain <record_id>` matters (it is what
    CLAUDE.md names this command by), so this callback exists purely to
    keep `explain` a required, named subcommand instead of collapsing.
    """


@app.command()
def explain(
    record_id: str,
    run_dir: Path = typer.Option(  # noqa: B008 -- this is Typer's own documented pattern
        DEFAULT_RUN_DIR,
        help="Directory holding ledger.json/settlement_report.json/bank_statement.json/rate_card.md.",
    ),
) -> None:
    """Print the complete causal chain for one transaction: what it was,
    which rules applied, what it should have netted, what it did net,
    which credit it landed in, and the proof."""
    ledger = load_ledger(run_dir)
    credits = load_bank_credits(run_dir)
    contract = load_contract(run_dir, default_provider())
    merchant_id = infer_merchant_id(ledger)
    calibration = load_calibration_artifact()

    typer.echo(explain_record(record_id, ledger, credits, contract, merchant_id, calibration=calibration))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
