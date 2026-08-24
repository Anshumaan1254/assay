"""Shared test doubles for the llm/ boundary tests.

Not a package fixture/conftest on purpose: these are plain imports, used
identically by every LLM-boundary test file, with no pytest magic involved.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

_Result = dict | Exception | Callable[[str, "type[BaseModel]", str], dict]


class RecordingProvider:
    """Captures exactly what the caller handed the model.

    `result` is a canned dict/Exception (contract_parser.py's usage: one
    call, one fixed answer), or a callable of (prompt, schema, model_hint)
    for tests that need a response shaped by what was actually asked --
    batched adjudication calls carry different residual_ids per call, so a
    single fixed dict cannot answer all of them correctly.
    """

    def __init__(self, result: _Result):
        self.calls: list[tuple[str, type[BaseModel], str]] = []
        self._result = result

    def generate_structured(self, prompt: str, schema: type[BaseModel], model_hint: str) -> dict:
        self.calls.append((prompt, schema, model_hint))
        if isinstance(self._result, Exception):
            raise self._result
        if callable(self._result):
            return self._result(prompt, schema, model_hint)
        return self._result
