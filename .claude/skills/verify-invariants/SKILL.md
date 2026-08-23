---
name: verify-invariants
description: Use before any commit to a money path, and at the end of every session. Runs the full guard suite.
---
Run in order and report a pass/fail table:

1. `python -m pytest tests/test_architecture.py -v` — boundary tests
2. `python scripts/guard_core.py $(git diff --name-only)` — changed files
3. `assay audit --profile clean` — assert unexplained == 0
4. `assay audit --profile clean && assay audit --profile clean` — assert both
   report hashes identical
5. `python -m pytest -q` — full suite

Any failure: stop, report which invariant broke, and do not proceed to commit.