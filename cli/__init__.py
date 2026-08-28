"""Typer app. The CLI is the product."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from cli.contract import contract_app
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
app.add_typer(contract_app, name="contract")


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
def audit(
    run_dir: Path = typer.Option(  # noqa: B008 -- this is Typer's own documented pattern
        DEFAULT_RUN_DIR,
        help="Directory holding ledger.json/settlement_report.json/bank_statement.json/rate_card.md.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the full report as canonical JSON."),
    adjudicate: bool = typer.Option(
        True, "--adjudicate/--no-adjudicate", help="Ask the model about residuals deterministic matching left."
    ),
) -> None:
    """Audit one settlement run end to end and print what it concluded.

    Takes a run directory, not a profile name. Resolving a profile name
    would mean generating data, which would mean `cli/` importing
    `datagen/` -- invariant 5. Generate first with
    `python -m datagen.cli generate --profile clean --seed 42`, then point
    this at `runs/clean-seed42`.

    Resumable: a process killed mid-audit can be re-run with the exact same
    arguments and will pick up from its last durable checkpoint rather than
    redoing completed work or double-posting -- see store/resumable.py.
    """
    from eval.determinism import NonDeterministicRun
    from store.resumable import CheckpointConflict, resume_or_run

    try:
        report = resume_or_run(
            run_dir,
            default_provider(),
            calibration=load_calibration_artifact(),
            adjudicate=adjudicate,
        )
    except (NonDeterministicRun, CheckpointConflict, ValueError) as error:
        # A refusal to score/resume is correct, intentional behaviour here
        # (see each exception's own docstring) -- only the presentation
        # changes, from a raw traceback to a clean, actionable message.
        typer.echo(f"assay audit: refused -- {error}", err=True)
        raise typer.Exit(code=1) from None

    if as_json:
        typer.echo(json.dumps(report.hash_payload(), indent=2, sort_keys=True))
        return

    for line in report.summary_lines():
        typer.echo(line)
    if report.clusters:
        typer.echo("")
        typer.echo("Top exceptions by money:")
        for cluster in report.clusters[:10]:
            typer.echo(
                f"  {cluster.total_impact.to_rupees_str():>14}  {cluster.discrepancy_class.value:<28}"
                f"  {cluster.count:>3} finding(s)  [{cluster.rule_id or 'no rule resolved'}]"
            )


@app.command()
def fetch(
    year: int = typer.Option(..., help="Four-digit year of the settlement month."),
    month: int = typer.Option(..., min=1, max=12, help="Month, 1-12."),
    day: int | None = typer.Option(None, min=1, max=31, help="Restrict to one day of that month."),
    merchant_id: str = typer.Option(..., help="Your merchant id, as it should appear on every record."),
    mcc: str = typer.Option(..., help="Your merchant category code. Razorpay's API does not report it."),
    rate_card: Path = typer.Option(  # noqa: B008 -- this is Typer's own documented pattern
        ..., help="Your negotiated rate card, as markdown. No gateway exposes this as data."
    ),
    out: Path = typer.Option(  # noqa: B008 -- this is Typer's own documented pattern
        ..., help="Run directory to write, ready for `assay audit --run-dir`."
    ),
) -> None:
    """Fetch one month of real Razorpay settlements into a run directory.

    Unlike `datagen`, this is importable from `cli/` -- `ingest/` reads a
    gateway's API, not planted ground truth, so invariant 5 does not apply
    to it. The import stays inside the function only to keep `assay
    audit`'s startup free of an HTTP stack it never uses.
    """
    from ingest.cli import fetch_run
    from ingest.razorpay import RazorpayUnavailable

    try:
        report = fetch_run(
            year=year, month=month, day=day, merchant_id=merchant_id,
            mcc=mcc, rate_card=rate_card, out_dir=out,
        )
    except RazorpayUnavailable as error:
        typer.echo(f"assay fetch: refused -- {error}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"wrote {out}")
    typer.echo(f"  mapped records:   {report.mapped_count}")
    typer.echo(f"  bank credits:     {len(report.bank_credits)}")
    typer.echo(f"  quarantined:      {len(report.quarantined)}")
    typer.echo(f"next: assay audit --run-dir {out}")


@app.command()
def eval(
    profiles: str = typer.Option("clean,realistic,stress", "--profiles", help="Comma-separated profile names."),
    seeds: str = typer.Option("", "--seeds", help="Comma-separated seeds; defaults to the held-out split."),
    out: Path = typer.Option(  # noqa: B008 -- this is Typer's own documented pattern
        Path("eval/results"), "--out", help="Where raw per-run results are written."
    ),
    evidence: bool = typer.Option(False, "--evidence", help="Regenerate EVIDENCE.md in place."),
    ablate: bool = typer.Option(True, "--ablate/--no-ablate", help="Run the ablation study."),
) -> None:
    """Score the engine against planted ground truth and write eval/results/.

    The import below is deliberately inside this function. `eval/` reads
    `datagen/` ground truth, and a module-level import here would put a
    path from the product's CLI package to the answers -- which invariant
    5 exists to prevent, and which tests/test_architecture.py asserts
    against by walking `cli/`'s module-level import graph.
    """
    from eval.cli import run_eval
    from eval.harness import SWEEP_SEEDS

    seed_list = [int(s) for s in seeds.split(",") if s.strip()] or list(SWEEP_SEEDS)
    run_eval(
        profiles=[p.strip() for p in profiles.split(",") if p.strip()],
        seeds=seed_list,
        out=out,
        write_evidence=evidence,
        ablate=ablate,
        echo=typer.echo,
    )


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
