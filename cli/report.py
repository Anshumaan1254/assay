"""`assay report <run_id>` and `assay replay <run_id>` -- reading back a
persisted audit report by run_id alone, and independently re-running an
audit to prove its report_hash reproduces (invariant 4, on demand).
"""

from __future__ import annotations

import html
from pathlib import Path

from cli.audit import AuditReport


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


def locate_run(run_id: str, store_path: Path | None = None) -> tuple[Path, Path, AuditReport]:
    """(run_dir, report_json_path, parsed report) for `run_id`, looked up
    purely from the audit store -- no `--run-dir` needed."""
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
