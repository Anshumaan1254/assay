"""`assay report <run_id>` and `assay replay <run_id>` -- reading back a
persisted audit report by run_id alone, and independently re-running an
audit to prove its report_hash reproduces (invariant 4, on demand).
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path

from cli.audit import AuditReport, run_audit
from core.lanes import CalibrationArtifact
from llm.provider import LLMProvider


def write_report_json(report: AuditReport) -> Path:
    """Persists the report next to the inputs that produced it, so `assay
    report`/`assay replay` can find it again from `run_id` alone via
    AuditRunRow.run_dir -- no `--run-dir` of their own."""
    target = Path(report.run_dir) / "report.json"
    target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return target


class RunNotFound(Exception):
    """No AuditRunRow exists for this run_id -- either it was never
    audited, or the store the caller is pointed at isn't the one it was
    audited into."""


class ReportArtifactMissing(Exception):
    """A run_id is a real, completed audit run, but its report.json is
    gone from disk -- moved, deleted, or the run directory was cleaned up
    after the fact."""


class ReportArtifactStale(Exception):
    """The report.json in this run's directory is some OTHER run's report.

    The run_id -> run_dir mapping lives in the audit store; the artifact
    lives in the run directory. Those are two places, and they drift: a
    re-audit of the same run_dir under different flags, or into a different
    ASSAY_STORE_PATH, rewrites the artifact while the original store row
    keeps the original hash. Serving the newer artifact under the older
    run_id would let `assay report` print a report whose own embedded hash
    contradicts the run it was asked for -- the exact substitution the
    hashing exists to make impossible, so it is refused rather than
    reconciled.
    """


def locate_run(run_id: str, store_path: Path | None = None) -> tuple[Path, Path, AuditReport]:
    """(run_dir, report_json_path, parsed report) for `run_id`, looked up
    purely from the audit store -- no `--run-dir` needed.

    The artifact's own `report_hash` is checked against the one the store
    recorded for this run_id. Both are written from the same report object
    in the same `assay audit` invocation, so in normal operation they
    always agree; a disagreement means the artifact on disk is not this
    run's, and is raised rather than served.
    """
    from sqlmodel import Session

    from store.models import AuditRunRow
    from store.resumable import default_store_path, get_engine

    engine = get_engine(store_path if store_path is not None else default_store_path())
    with Session(engine) as session:
        row = session.get(AuditRunRow, run_id)

    if row is None:
        raise RunNotFound(
            f"no run '{run_id}' in the audit store -- run 'assay audit --run-dir <dir>' first"
        )

    run_dir = Path(row.run_dir)
    report_json_path = run_dir / "report.json"
    if not report_json_path.is_file():
        raise ReportArtifactMissing(
            f"run '{run_id}' is recorded in the audit store, but {report_json_path} is missing -- "
            f"re-run 'assay audit --run-dir {run_dir}' to regenerate it"
        )

    report = AuditReport.model_validate_json(report_json_path.read_text(encoding="utf-8"))
    if report.report_hash != row.report_hash:
        raise ReportArtifactStale(
            f"{report_json_path} carries report_hash {report.report_hash}, but the audit store "
            f"records {row.report_hash} for run '{run_id}' -- this artifact is not that run's "
            f"report. Re-run 'assay audit --run-dir {run_dir}' to regenerate it."
        )
    return run_dir, report_json_path, report


def render_table(report: AuditReport) -> str:
    """`assay audit`'s own default output and `assay report --format table`
    share this, so the two can never drift into two different summaries of
    the same report."""
    lines = list(report.summary_lines())
    if report.clusters:
        lines.append("")
        lines.append("Top exceptions by money:")
        for cluster in report.clusters[:10]:
            lines.append(
                f"  {cluster.total_impact.to_rupees_str():>14}  {cluster.discrepancy_class.value:<28}"
                f"  {cluster.count:>3} finding(s)  [{cluster.rule_id or 'no rule resolved'}]"
            )
    return "\n".join(lines)


@dataclass(frozen=True)
class ReplayResult:
    match: bool
    stored_hash: str
    recomputed_hash: str
    run_dir: str


def replay_run(
    run_id: str,
    provider: LLMProvider,
    *,
    calibration: CalibrationArtifact | None = None,
    store_path: Path | None = None,
) -> ReplayResult:
    """Independently re-run `run_id`'s audit from its original run_dir and
    prove its report_hash reproduces -- invariant 4, on demand. Calls
    `run_audit` directly, not `resume_or_run`: `run_audit` is the pure,
    uncheckpointed entry point, kept exactly so a second, independent call
    can prove determinism -- going through the checkpoint would just replay
    it back and prove nothing.

    `provider`/`calibration` are taken as parameters, not resolved here via
    `cli.loaders.default_provider()` -- a caller in cli/__init__.py already
    has its own, monkeypatchable copy of that name imported at module
    scope (the same one `assay audit`/`assay explain` use); resolving a
    second, independent copy here would silently bypass a test's stub on
    the first and, on a machine with no GEMINI_API_KEY and an incomplete
    cache, could reach a real network call instead of the clean refusal
    every other command gives.
    """
    run_dir, _report_json_path, original = locate_run(run_id, store_path)

    # Not asking (provider=None) and asking-but-being-refused are both
    # recorded on the original report, and neither should silently become
    # the other kind of run on replay -- see AuditReport's own docstring.
    adjudicate = original.adjudication is not None or original.adjudication_degraded

    # merchant_id is deliberately NOT passed through from `original` --
    # left None so run_audit re-derives it via infer_merchant_id(ledger),
    # the same as the original `assay audit` call did. Feeding the stored
    # value back in would make replay trust it instead of re-proving it,
    # narrowing what invariant 4 actually checks.
    recomputed = run_audit(
        run_dir,
        provider,
        calibration=calibration,
        adjudicate=adjudicate,
    )

    return ReplayResult(
        match=recomputed.report_hash == original.report_hash,
        stored_hash=original.report_hash,
        recomputed_hash=recomputed.report_hash,
        run_dir=str(run_dir),
    )


def render_html(report: AuditReport) -> str:
    """Hand-rolled, no templating dependency -- consistent with this UI's
    "plain and fast, no decoration" brief. Every free-text field (cluster
    labels, rule ids -- ultimately derived from rate-card/bank-statement
    text) is escaped before interpolation, since this can render in a real
    browser."""
    esc = html.escape
    rows = "".join(
        f"<tr><td>{esc(c.total_impact.to_rupees_str())}</td>"
        f"<td>{esc(c.discrepancy_class.value)}</td>"
        f"<td>{c.count}</td>"
        f"<td>{esc(c.rule_id or 'no rule resolved')}</td></tr>"
        for c in report.clusters[:10]
    )
    summary = "\n".join(f"<div>{esc(line)}</div>" for line in report.summary_lines())
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{esc(report.audit_run_id)}</title></head>
<body>
<h1>{esc(report.report_hash)}</h1>
<section>{summary}</section>
<table border="1" cellpadding="4">
<thead><tr><th>impact</th><th>class</th><th>count</th><th>rule</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</body></html>
"""
