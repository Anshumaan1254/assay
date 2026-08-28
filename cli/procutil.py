"""Shared subprocess helper for cli/ commands that shell out to a separate
process rather than importing it in-process -- currently `assay generate`
(datagen/ is quarantined, invariant 5) and `assay chaos` (reuses tested
pytest/report-formatting logic instead of forking it).
"""

from __future__ import annotations

import subprocess


def run_and_stream(cmd: list[str]) -> int:
    """Run `cmd` with stdio inherited, so the child's own output prints
    live, and return its exit code without raising on a nonzero one --
    callers decide what a failure means."""
    return subprocess.run(cmd, check=False).returncode
