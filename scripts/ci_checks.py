"""The assertions .github/workflows/ci.yml makes, as testable code.

Three gates in the workflow need more than a shell one-liner: reading a
field out of a signed report, asserting a zero-discrepancy month really
reconciled to zero, and holding the container to its size budget. Inline
`python - <<PY` blocks in YAML would work and could never be run or tested
locally, so they live here instead.

Standalone by design -- no imports from core/, cli/ or eval/ -- so the
image-size check runs in the docker job, which installs nothing at all.
Same discipline as scripts/check_evidence.py.

    python scripts/ci_checks.py field runs/x/report.json report_hash
    python scripts/ci_checks.py reconciled runs/clean-seed42/report.json
    python scripts/ci_checks.py image-size assay --budget-mb 500
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# `docker history` prints human sizes: "361MB", "7.93MB", "16.4kB", "0B".
SIZE = re.compile(r"^\s*([0-9.]+)\s*([kMG]?B)\s*$")
UNIT_MB = {"B": 1 / 1_048_576, "kB": 1 / 1024, "MB": 1.0, "GB": 1024.0}


def load(report_path: Path) -> dict:
    if not report_path.is_file():
        raise SystemExit(f"ci_checks: {report_path} does not exist -- did `assay audit` run?")
    return json.loads(report_path.read_text(encoding="utf-8"))


def cmd_field(args: argparse.Namespace) -> int:
    """Print one top-level field, for the workflow to capture."""
    report = load(args.report)
    if args.name not in report:
        raise SystemExit(f"ci_checks: {args.report} has no field {args.name!r}")
    print(report[args.name])
    return 0


def cmd_reconciled(args: argparse.Namespace) -> int:
    """A clean-profile month plants nothing, so invariant 3 must close exactly."""
    report = load(args.report)

    # total_unaccounted_paise, not total_unexplained_paise. A lost settlement
    # batch produces no bank credit, hence no residual, and reads as
    # perfectly clean on the narrower field -- core/conserve.py says so in
    # its own comment, and eval/sweep.py gates on this wider one.
    unexplained = report["total_unexplained_paise"]
    unclaimed = report["unclaimed_paise"]
    unaccounted = report["total_unaccounted_paise"]

    print(f"unexplained : {unexplained} paise")
    print(f"unclaimed   : {unclaimed} paise")
    print(f"unaccounted : {unaccounted} paise")

    if unaccounted != 0:
        print(
            f"FAIL: the clean profile left {unaccounted} paise unaccounted. "
            "A month with nothing planted must reconcile to exactly zero -- "
            "not within tolerance, not rounded. Invariant 3.",
            file=sys.stderr,
        )
        return 1

    print("OK: clean profile reconciles to exactly zero")
    return 0


def image_size_mb(tag: str) -> float:
    """Sum of the image's uncompressed layer sizes, in MB.

    Read from `docker history` rather than `docker image inspect`, whose
    .Size means different things under the containerd image store (the
    compressed content size) and the classic overlay2 driver (this sum).
    """
    result = subprocess.run(
        ["docker", "history", tag, "--format", "{{.Size}}"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"ci_checks: docker history {tag} failed -- {result.stderr.strip()}")

    total = 0.0
    for line in result.stdout.splitlines():
        match = SIZE.match(line)
        if match:
            total += float(match.group(1)) * UNIT_MB[match.group(2)]
    return total


def cmd_image_size(args: argparse.Namespace) -> int:
    size = image_size_mb(args.tag)
    print(f"image size: {size:.1f} MB (budget {args.budget_mb} MB)")

    if size > args.budget_mb:
        print(
            f"FAIL: {args.tag} is {size - args.budget_mb:.1f} MB over budget.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: {args.budget_mb - size:.1f} MB of headroom")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_field = sub.add_parser("field", help="print one top-level field of a report")
    p_field.add_argument("report", type=Path)
    p_field.add_argument("name")
    p_field.set_defaults(func=cmd_field)

    p_rec = sub.add_parser("reconciled", help="fail unless the report is fully accounted for")
    p_rec.add_argument("report", type=Path)
    p_rec.set_defaults(func=cmd_reconciled)

    p_size = sub.add_parser("image-size", help="fail if a docker image exceeds its budget")
    p_size.add_argument("tag")
    p_size.add_argument("--budget-mb", type=float, default=500.0)
    p_size.set_defaults(func=cmd_image_size)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
