"""`assay eval` / `python -m eval.cli`.

Two entry points, one implementation. The module-level one exists for the
same reason `datagen/cli.py` has its own: `eval/` imports `datagen/`, and a
package that reads ground truth should be runnable without going through
the product's CLI at all.

`assay eval` reaches `run_eval` through a function-local import inside the
command body, so `cli/`'s module-level import graph never touches
`datagen/` -- tests/test_architecture.py walks that graph and asserts it.
Invariant 5's letter forbids `cli/` importing `datagen/`; its spirit is
that the audit engine must never be able to see the answers. The audit path
(`assay audit`, `cli/audit.py`, everything under `core/`) has no path to
`datagen/` at all, module-level or otherwise. What `assay eval` adds is a
command that deliberately reads ground truth, in a package built to, which
is the one place that is the whole point.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, help="Evaluation harness. Reads ground truth -- see eval/cli.py.")

DEFAULT_RESULTS_DIR = Path("eval/results")


def run_eval(
    *,
    profiles: list[str],
    seeds: list[int],
    out: Path,
    write_evidence: bool,
    ablate: bool,
    echo=print,
) -> Path:
    """Run the sweep, persist every raw result, and optionally regenerate
    EVIDENCE.md and the reliability diagrams.

    Imported lazily by `cli/__init__.py`; import it here at module level
    because this module is already inside `eval/`.
    """
    from eval.diagram import write_png, write_svg
    from eval.evidence import write as write_evidence_file
    from eval.sweep import default_provider_factory, run_sweep

    out.mkdir(parents=True, exist_ok=True)

    def progress(spec) -> None:
        echo(f"  running {spec.run_id} ...")

    echo(f"sweeping {len(profiles)} profiles x {len(seeds)} seeds = {len(profiles) * len(seeds)} runs")
    report = run_sweep(
        default_provider_factory,
        profiles=profiles,
        seeds=seeds,
        ablate=ablate,
        progress=progress,
    )

    for result in report.runs:
        (out / f"{result.run_id}.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    (out / "sweep.json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    echo(f"wrote {len(report.runs)} run results + sweep.json to {out}/")

    svg = write_svg(
        report.calibration.bins,
        ece=report.calibration.expected_calibration_error,
        brier=report.calibration.brier_score,
        total=report.calibration.total,
    )
    png = write_png(
        report.calibration.bins,
        ece=report.calibration.expected_calibration_error,
        brier=report.calibration.brier_score,
        total=report.calibration.total,
    )
    echo(f"wrote {svg}" + (f" and {png}" if png else " (matplotlib unavailable; PNG skipped)"))

    evidence_path = DEFAULT_RESULTS_DIR / "EVIDENCE.preview.md"
    if write_evidence:
        evidence_path = write_evidence_file(report)
    else:
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        write_evidence_file(report, evidence_path)
    echo(f"wrote {evidence_path}")

    detection = report.detection
    echo("")
    echo(f"  value-weighted recall : {detection.value_recall * 100:.1f}%")
    echo(f"  count recall          : {detection.count_recall * 100:.1f}%")
    echo(f"  false positives       : {detection.false_positive_findings} "
         f"({detection.false_positive_paise / 100:,.2f} rupees falsely claimed)")
    echo(f"  determinism           : {'MATCH' if report.determinism.matches else 'MISMATCH'}")
    echo(f"  clean profile         : {report.clean_profile_unaccounted_paise} paise unaccounted")
    return evidence_path


@app.command()
def sweep(
    profiles: str = typer.Option("clean,realistic,stress", "--profiles", help="Comma-separated profile names."),
    seeds: str = typer.Option("", "--seeds", help="Comma-separated seeds; defaults to the held-out split."),
    out: Path = typer.Option(  # noqa: B008 -- this is Typer's own documented pattern
        DEFAULT_RESULTS_DIR, "--out", help="Where raw per-run results are written."
    ),
    evidence: bool = typer.Option(False, "--evidence", help="Regenerate EVIDENCE.md in place."),
    ablate: bool = typer.Option(True, "--ablate/--no-ablate", help="Run the §13 ablation study."),
) -> None:
    """Run the evaluation sweep and write eval/results/ (and, with
    --evidence, EVIDENCE.md plus docs/reliability.{svg,png})."""
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
def render(
    results: Path = typer.Option(  # noqa: B008 -- this is Typer's own documented pattern
        DEFAULT_RESULTS_DIR / "sweep.json", "--results", help="A sweep.json written by `sweep`."
    ),
    evidence: bool = typer.Option(False, "--evidence", help="Write EVIDENCE.md in place."),
) -> None:
    """Re-render EVIDENCE.md and the diagrams from an existing sweep.

    The document and the measurements are separate concerns, and this is
    the seam between them: wording, table layout and section order can be
    iterated on in a second, against numbers that took twenty minutes and
    a pile of API calls to produce. It also means the committed EVIDENCE.md
    is provably a rendering of the committed `eval/results/sweep.json`, not
    of some other run.
    """
    from eval.diagram import write_png, write_svg
    from eval.evidence import EVIDENCE_PATH
    from eval.evidence import write as write_evidence_file
    from eval.sweep import SweepReport

    report = SweepReport.model_validate_json(results.read_text(encoding="utf-8"))
    write_svg(
        report.calibration.bins,
        ece=report.calibration.expected_calibration_error,
        brier=report.calibration.brier_score,
        total=report.calibration.total,
    )
    write_png(
        report.calibration.bins,
        ece=report.calibration.expected_calibration_error,
        brier=report.calibration.brier_score,
        total=report.calibration.total,
    )
    path = EVIDENCE_PATH if evidence else DEFAULT_RESULTS_DIR / "EVIDENCE.preview.md"
    write_evidence_file(report, path)
    typer.echo(f"rendered {path} from {results}")


@app.command()
def summarize(
    results: Path = typer.Option(  # noqa: B008 -- this is Typer's own documented pattern
        DEFAULT_RESULTS_DIR / "sweep.json", "--results"
    ),
) -> None:
    """Print the headline numbers from an existing sweep without re-running it."""
    payload = json.loads(results.read_text(encoding="utf-8"))
    detection = payload["detection"]
    typer.echo(f"runs                  : {len(payload['runs'])}")
    typer.echo(f"findings              : {detection['findings_total']}")
    typer.echo(f"false positives       : {detection['false_positive_findings']}")
    typer.echo(f"rupees falsely claimed: {detection['false_positive_paise'] / 100:,.2f}")
    typer.echo(f"planted               : {detection['planted_paise'] / 100:,.2f}")
    typer.echo(f"detected              : {detection['detected_paise'] / 100:,.2f}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
