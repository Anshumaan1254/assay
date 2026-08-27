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


class MalformedResponse(ProviderUnavailable):
    """The provider answered, but the response body itself was unusable --
    empty, not valid JSON, or JSON that wasn't an object -- as opposed to a
    rate limit or a missing API key. IS-A ProviderUnavailable, so every
    existing `except ProviderUnavailable` catch site and every existing
    `pytest.raises(ProviderUnavailable, ...)` keeps working unchanged; this
    subclass exists only so a retry wrapper can catch *this* failure mode
    specifically, without also intercepting (and re-retrying on top of) a
    429-exhaustion GeminiProvider has already decided is not worth retrying
    again for an unrelated reason.
    """


class TokenUsage(BaseModel):
    """What one call cost, in tokens.

    `estimated` is the honest half of this type. A provider that reports its
    own usage sets it False; a figure derived from byte length — the only
    thing available for a cache entry recorded before usage was tracked —
    sets it True, and EVIDENCE.md prints the two differently. A token count
    that cannot say which kind it is would be worse than none.
    """

    prompt_tokens: int = 0
    response_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False


# Roughly four characters per token. Only ever applied to a cache entry
# whose real usage was never recorded, and every number derived from it is
# labelled `estimated` all the way out to the report.
_CHARS_PER_TOKEN = 4


def estimate_usage(prompt: str, response_text: str) -> TokenUsage:
    prompt_tokens = len(prompt) // _CHARS_PER_TOKEN
    response_tokens = len(response_text) // _CHARS_PER_TOKEN
    return TokenUsage(
        prompt_tokens=prompt_tokens,
        response_tokens=response_tokens,
        total_tokens=prompt_tokens + response_tokens,
        estimated=True,
    )


# A provider MAY additionally expose, as plain attributes, `last_usage:
# TokenUsage | None` and the counters llm/telemetry.py reads. They are
# deliberately NOT part of the Protocol below: `LLMProvider` is
# `runtime_checkable`, and a Protocol carrying non-method members cannot be
# used with isinstance() at all. Telemetry is read with getattr instead, so
# a third-party provider that tracks nothing still satisfies the boundary.
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
