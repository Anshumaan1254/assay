PYTHON ?= D:/CondaEnvs/ai/python.exe

.PHONY: install test lint guard demo evidence eval chaos ui ui-build

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check .

guard:
	$(PYTHON) scripts/guard_core.py $$(git ls-files '*.py')
	$(PYTHON) scripts/check_evidence.py

# The sweep, without touching EVIDENCE.md. Writes eval/results/ and a
# preview document, so a change to the harness can be inspected before it
# becomes the committed report.
eval:
	$(PYTHON) -m eval.cli sweep

# Regenerates EVIDENCE.md in place from a real run, plus the reliability
# diagrams in docs/. EVIDENCE.md is GENERATED -- never hand-edited; `make
# guard` fails if it was.
evidence:
	$(PYTHON) -m eval.cli sweep --evidence

# End to end from a clean clone: generate the canonical realistic-profile
# run, compile and pin its contract, audit it, and score a matching eval
# run -- all through the committed .llm_cache/, so no GEMINI_API_KEY is
# needed. Deliberately NOT the full eval sweep (see `make eval`): every
# profile x every seed x --ablate does not fit a two-minute budget even
# fully cached, so demo scores one profile/seed matching the run it just
# audited, --no-ablate, into its own eval/results/demo/ subpath rather
# than the committed top-level eval/results/.
demo:
	$(PYTHON) -m cli generate --profile realistic --seed 42
	$(PYTHON) -m cli contract compile runs/realistic-seed42/rate_card.md
	$(PYTHON) -m cli audit --run-dir runs/realistic-seed42
	$(PYTHON) -m cli eval --profiles realistic --seeds 42 --no-ablate --no-diagrams --out eval/results/demo

# Builds the reviewer front end into reviewer/web/dist/, which reviewer/api.py
# mounts at "/" when present. Requires node; the Python side works without it
# (the API is fully usable on its own, which is how the Vite dev server
# consumes it).
ui-build:
	cd reviewer/web && npm install && npm run build

# Serves the built UI and the read-only API on one origin. Audit something
# first -- the reviewer reads runs the engine has already produced:
#   make demo   (or)   assay audit --run-dir runs/realistic-seed42
ui: ui-build
	$(PYTHON) -m uvicorn reviewer.api:app --host 127.0.0.1 --port 8000

# Runs every failure-injection scenario and prints a pass/fail table read
# back from the structured incident log each scenario writes -- see
# chaos/incident.py. Real subprocess kills (C02) make this slower than the
# rest of the suite; --timeout is generous rather than tight.
chaos:
	$(PYTHON) -m pytest chaos/ -q --timeout=600
	$(PYTHON) scripts/chaos_report.py
