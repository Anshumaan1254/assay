PYTHON ?= D:/CondaEnvs/ai/python.exe

.PHONY: install test lint guard demo

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check .

guard:
	$(PYTHON) scripts/guard_core.py $$(git ls-files '*.py')

demo:
	@echo "demo: not implemented yet"
