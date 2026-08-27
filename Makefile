PYTHON ?= D:/CondaEnvs/ai/python.exe

.PHONY: install test lint guard demo evidence eval chaos

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

demo:
	@echo "demo: not implemented yet"

# Runs every failure-injection scenario and prints a pass/fail table read
# back from the structured incident log each scenario writes -- see
# chaos/incident.py. Real subprocess kills (C02) make this slower than the
# rest of the suite; --timeout is generous rather than tight.
chaos:
	$(PYTHON) -m pytest chaos/ -q --timeout=600
	$(PYTHON) scripts/chaos_report.py
