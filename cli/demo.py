"""`assay demo` -- the end-to-end run from a clean clone, as one command.

Four steps: generate the canonical realistic-profile month, compile and pin
its contract, audit it, and score a matching eval run. Everything is served
from the committed `.llm_cache/`, so no GEMINI_API_KEY and no network.

Every step runs via subprocess, for the same reason `assay generate` does:
step one is the generator, and `datagen/` is quarantined (invariant 5), so
`cli/` must not grow a module-level path to it. Subprocessing all four
rather than only the first keeps the sequence uniform and lets each step's
own output stream live.

This is the single definition of "the demo" -- Makefile's `demo` target,
the README quickstart and the Docker image's `assay demo` all reach these
same four lines rather than each keeping its own copy.
"""

from __future__ import annotations

import sys

from cli.procutil import run_and_stream

RUN_DIR = "runs/realistic-seed42"

DEMO_STEPS: tuple[tuple[str, ...], ...] = (
    ("generate", "--profile", "realistic", "--seed", "42"),
    ("contract", "compile", f"{RUN_DIR}/rate_card.md"),
    # Relative, never absolute: `run_dir` is part of the report's hash
    # payload, so an absolute path would make report_hash depend on where
    # the repository happens to sit on disk. See DECISIONS.md 2026-08-30
    # 00:20, and scripts/vercel_reviewer_build.py's RUN_DIR_ARG.
    ("audit", "--run-dir", RUN_DIR),
    # Deliberately not the full sweep (`make eval`): every profile x every
    # seed x --ablate does not fit a demo's time budget even fully cached.
    # One profile/seed matching the run just audited, into its own
    # eval/results/demo/ rather than the committed top-level results.
    ("eval", "--profiles", "realistic", "--seeds", "42",
     "--no-ablate", "--no-diagrams", "--out", "eval/results/demo"),
)


def run_demo() -> int:
    """Run the four steps in order, stopping at the first failure.

    Stops rather than continuing, unlike `assay chaos`: each step consumes
    what the previous one wrote, so running step three after step two
    failed audits a run whose contract was never compiled and reports a
    confusing downstream error instead of the real one. Returns the exit
    code of the first step that failed, or 0.
    """
    for step in DEMO_STEPS:
        code = run_and_stream([sys.executable, "-m", "cli", *step])
        if code:
            return code
    return 0
