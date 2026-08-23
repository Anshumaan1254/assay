"""Always-unavailable provider. Used by chaos scenario 9 to prove the audit
still produces a number when the LLM is entirely absent.
"""

from __future__ import annotations

from pydantic import BaseModel

from llm.provider import ProviderUnavailable


class NullProvider:
    def generate_structured(self, prompt: str, schema: type[BaseModel], model_hint: str) -> dict:
        raise ProviderUnavailable("NullProvider never provides a response")
