---
name: new-discrepancy-class
description: Use when adding a new discrepancy class to the taxonomy. Ensures generator, detector, test, and eval entry all land together.
---
Adding a discrepancy class touches five places. Do all five or none:

1. `core/exceptions.py` — add the enum member with a docstring explaining the
   real-world cause.
2. `datagen/inject.py` — add the injector, with a configurable rate, recording
   exact ground truth (record ids, class, rupee impact).
3. `core/verify.py` — add the deterministic detection rule. Never a model.
4. `tests/` — a test that generates exactly one instance and asserts detection.
5. `eval/metrics.py` — add the class to per-class reporting so EVIDENCE.md
   picks it up automatically.

Then regenerate EVIDENCE.md and confirm the new class appears with non-zero
support. Report the new per-class precision and recall.