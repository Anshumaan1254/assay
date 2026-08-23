"""Typer CLI for the synthetic settlement-month generator. Kept OUT of
cli/ (the product's own CLI package) so quarantine can never be
accidentally violated by a shared import -- this module is the only place
in datagen/ meant to be invoked directly (`python -m datagen.cli generate
...`), never imported by anything under core/, llm/, or cli/.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from random import Random

import typer

from core.models import IST
from datagen.config import GenerationConfig, load_profile
from datagen.ground_truth import GroundTruth
from datagen.inject import apply_discrepancies, apply_silent_corruption
from datagen.ratecard import default_rate_card
from datagen.summary import build_summary, render_summary
from datagen.world import build_true_world
from datagen.writer import write_ground_truth, write_run

app = typer.Typer(add_completion=False, help="Synthetic settlement-month generator. QUARANTINED -- see datagen/__init__.py.")

_BUILTIN_PROFILE_NAMES = ("clean", "realistic", "stress")


@app.command()
def generate(
    profile: str = typer.Option("realistic", "--profile", help="clean | realistic | stress, or a path to a custom YAML"),
    seed: int = typer.Option(..., "--seed", help="RNG seed -- required, determinism depends on it"),
    silent_corruption: bool = typer.Option(False, "--silent-corruption"),
    month: str = typer.Option("2026-07", "--month", help="YYYY-MM"),
    out: Path = typer.Option(Path("runs"), "--out", help="Parent dir; writes out/<run_id>/"),
    truth_out: Path = typer.Option(Path("truth/ground_truth.json"), "--truth-out"),
    run_id: str = typer.Option(None, "--run-id", help="Override the derived run_id"),
) -> None:
    """Generate one settlement month: the true world, a copy with the
    requested profile's discrepancies planted, an optional silent 1-paise
    corruption, and the ground truth that describes every deviation from
    truth. Prints summary statistics and the full ground-truth manifest."""
    config = GenerationConfig(month=month)
    rate_card = default_rate_card(month)

    true_world = build_true_world(config, rate_card, Random(seed))
    injection_profile = load_profile(profile)
    reported_world, discrepancies, flags = apply_discrepancies(true_world, rate_card, injection_profile, Random(seed + 1))

    silent_corruption_record = None
    if silent_corruption:
        reported_world, silent_corruption_record = apply_silent_corruption(reported_world, Random(seed + 2))

    profile_name = profile if profile in _BUILTIN_PROFILE_NAMES else Path(profile).stem
    resolved_run_id = run_id or f"{profile_name}-seed{seed}" + ("-corrupt" if silent_corruption else "")
    counts = injection_profile.model_dump()
    generated_at = datetime.now(IST)

    ground_truth = GroundTruth(
        run_id=resolved_run_id,
        seed=seed,
        profile=profile_name,
        generated_at=generated_at,
        silent_corruption=silent_corruption_record,
        discrepancies=discrepancies,
        data_quality_flags=flags,
        counts=counts,
    )

    run_out_dir = out / resolved_run_id
    manifest = {
        "run_id": resolved_run_id,
        "seed": seed,
        "profile": profile_name,
        "month": month,
        "silent_corruption": silent_corruption,
        "generated_at": generated_at.isoformat(),
        "counts": counts,
    }
    write_run(reported_world, rate_card, manifest=manifest, out_dir=run_out_dir, merchant_id=config.merchant_id)
    write_ground_truth(ground_truth, truth_out)

    summary = build_summary(reported_world, ground_truth)

    typer.echo(f"run_id: {resolved_run_id}")
    typer.echo(f"seed: {seed}  profile: {profile_name}  month: {month}  silent_corruption: {silent_corruption}")
    typer.echo("")
    typer.echo(render_summary(summary))
    typer.echo("")
    typer.echo("Ground truth manifest:")
    typer.echo(ground_truth.model_dump_json(indent=2))
    typer.echo("")
    typer.echo(f"Wrote: {run_out_dir}/")
    typer.echo(f"Wrote: {truth_out}")


if __name__ == "__main__":
    app()
