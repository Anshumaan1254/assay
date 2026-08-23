"""The LLMProvider boundary. Every model call in this project goes through
this Protocol, so swapping providers is a one-line config change, not a
rewrite.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class ProviderUnavailable(Exception):
    """No structured response could be obtained.

    Raised when a request is rate-limited past its retries, the provider is
    missing required configuration (e.g. an API key), or a provider (like
    NullProvider) is intentionally disabled. Callers never need to catch
    provider-SDK-specific exceptions — every failure mode funnels through
    this one.
    """


@runtime_checkable
class LLMProvider(Protocol):
    def generate_structured(self, prompt: str, schema: type[BaseModel], model_hint: str) -> dict:
        """Return a JSON-schema-conformant dict for `prompt`, shaped by `schema`.

        The caller is responsible for validating the returned dict into an
        actual Pydantic model instance and reference-checking any IDs it
        cites — this method only guarantees the response was constrained to
        the schema's shape, not that its content is trustworthy.
        """
        ...
