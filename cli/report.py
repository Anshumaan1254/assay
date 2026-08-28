"""`assay report <run_id>` and `assay replay <run_id>` -- reading back a
persisted audit report by run_id alone, and independently re-running an
audit to prove its report_hash reproduces (invariant 4, on demand).
"""

from __future__ import annotations

from pathlib import Path

from cli.audit import AuditReport


def write_report_json(report: AuditReport) -> Path:
    """Persists the report next to the inputs that produced it, so `assay
    report`/`assay replay` can find it again from `run_id` alone via
    AuditRunRow.run_dir -- no `--run-dir` of their own."""
    target = Path(report.run_dir) / "report.json"
    target.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return target
