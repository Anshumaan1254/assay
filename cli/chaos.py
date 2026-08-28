"""`assay chaos` -- runs every failure-injection scenario, then prints the
pass/fail table read back from the incidents each scenario recorded.

Both steps run via subprocess, mirroring `assay generate`: chaos/ itself
isn't quarantined (it imports no datagen), but scripts/chaos_report.py
isn't a package (no __init__.py) and owns the tested table-formatting
logic -- subprocessing it reuses that logic rather than forking it.
"""

from __future__ import annotations

import sys

from cli.procutil import run_and_stream


def run_chaos_suite() -> int:
    """Runs `pytest chaos/`, then `scripts/chaos_report.py` unconditionally
    -- even on failure. `make chaos`'s raw two-line recipe stops at the
    first nonzero exit (Make's default per-line failure semantics), so if
    any scenario fails, the one artifact that says *which* scenario failed
    never prints. Returns nonzero iff either step failed.
    """
    suite_code = run_and_stream([sys.executable, "-m", "pytest", "chaos/", "-q", "--timeout=600"])
    report_code = run_and_stream([sys.executable, "scripts/chaos_report.py"])
    return suite_code or report_code
