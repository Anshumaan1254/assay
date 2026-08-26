---
name: verify-invariants
description: Use before any commit to a money path, and at the end of every session. Runs the full guard suite.
---
Run in order and report a pass/fail table:

1. `python -m pytest tests/test_architecture.py -v` — boundary tests
2. `python scripts/guard_core.py $(git diff --name-only)` — changed files
3. `assay audit --run-dir runs/clean-seed42` — assert `unexplained`,
   `unclaimed` and `total unaccounted` are all exactly `0.00`
4. `assay audit --run-dir runs/clean-seed42` twice — assert both report
   hashes are identical
5. `python scripts/check_evidence.py` — EVIDENCE.md is generated, not
   hand-edited
6. `python -m pytest -q` — full suite

Any failure: stop, report which invariant broke, and do not proceed to commit.

**Note on step 3/4.** `assay audit` takes a run directory, not a profile
name: resolving a profile name would mean generating data, which would mean
`cli/` importing `datagen/` — invariant 5. If `runs/clean-seed42` is absent,
generate it first with:

    python -m datagen.cli --profile clean --seed 42 --truth-out truth/clean-seed42.ground_truth.json
