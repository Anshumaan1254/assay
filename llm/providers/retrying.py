"""Bounded retry with backoff for a malformed LLM response.

A provider's `generate_structured` can fail two structurally different ways:
a 429 (or another API error) before any content was even produced, and a
response that came back but whose body is unusable -- empty, not valid
JSON, or JSON that isn't an object. `GeminiProvider` already retries the
first kind (rate limiting is a vendor concern its own config already
governs). This wrapper retries the second kind, and only the second kind:
`MalformedResponse` is the one failure both loops could plausibly clear on
a retry (a glitched response), whereas a schema-valid-JSON-but-domain-
invalid response (a rejected AdjudicationBatchResponse/RateCardParse/
NarrationResponse) is a different failure entirely -- it happens one layer
up, inside the boundary modules, after generate_structured has already
returned successfully, so this wrapper never even sees it and there is
nothing here to special-case for it.

An `LLMProvider`-conforming wrapper, not a change to GeminiProvider itself:
"the model boundary is an interface, not a vendor" has to stay true for any
future provider, not just Gemini. Composition order matters -- wrap the
*most robust* provider with the cache, not the other way around:

    CachedProvider(RetryingProvider(GeminiProvider()))

so every retry attempt happens inside one CachedProvider-observed call, and
only a final successful result -- never an intermediate malformed one -- is
ever written to the committed .llm_cache/.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pydantic import BaseModel

from llm.provider import MalformedResponse, ProviderUnavailable
from llm.providers.backoff import sleep_with_backoff


class RetryConfig(BaseModel):
    max_retries: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> RetryConfig:
        # Same LLM_* names GeminiConfig.from_env() already reads: retry and
        # backoff are not vendor concepts, so both loops share one config
        # surface rather than each inventing their own.
        load_dotenv()
        return cls(
            max_retries=int(os.environ.get("LLM_MAX_RETRIES", cls.model_fields["max_retries"].default)),
            backoff_base_seconds=float(
                os.environ.get("LLM_RETRY_BACKOFF_BASE", cls.model_fields["backoff_base_seconds"].default)
            ),
        )


class RetryingProvider:
    def __init__(self, inner, config: RetryConfig | None = None):
        self._inner = inner
        self._config = config or RetryConfig.from_env()

    def generate_structured(self, prompt: str, schema: type[BaseModel], model_hint: str) -> dict:
        last_error: MalformedResponse | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                return self._inner.generate_structured(prompt, schema, model_hint)
            except MalformedResponse as error:
                last_error = error
                if attempt >= self._config.max_retries:
                    break
                sleep_with_backoff(attempt, self._config.backoff_base_seconds, self._config.backoff_max_seconds)

        raise ProviderUnavailable(
            f"still receiving a malformed response after {self._config.max_retries} retries"
        ) from last_error

    @property
    def last_usage(self):
        # This wrapper tracks no telemetry of its own -- llm/telemetry.py's
        # _chain() walks `_inner` and reads every counter (live_calls,
        # rate_limited, backoff_seconds, ...) straight off GeminiProvider
        # underneath, which is correct: this class doesn't own any of them,
        # so it must not claim to via a blanket __getattr__. last_usage is
        # the one exception -- CachedProvider reads it directly off
        # whatever it wraps immediately after a successful call, so it has
        # to reach through this layer rather than skip it.
        return getattr(self._inner, "last_usage", None)
