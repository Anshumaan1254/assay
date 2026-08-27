"""Gemini implementation of LLMProvider, using `models.generate_content`
with a `response_schema` for constrained decoding — never prompt-and-pray.

WARNING — this module's worst failure modes are invisible to its own tests.
Every test in `tests/test_provider.py` fakes the client, so a schema-dialect
mismatch (`additionalProperties`, `$defs`/`$ref`, a renamed request field)
passes a green suite and fails only against the live endpoint. That is not
hypothetical: see the 2026-08-24 15:10 entry in DECISIONS.md. The schema was
passed under a key the API silently ignored, so every call this project made
returned unconstrained prose, and nothing at the call site could tell —
`generate_structured` still returned a dict, and the suite still passed. A
live call was the only thing that found it.

The disk cache compounds this. `CachedProvider` keys on
sha256(prompt + schema + model) and is committed to the repo so replays need
no API key, which also means a replay never re-contacts the API. A cached
response that looks correct proves only that it was correct under whatever
contract held when it was recorded; if the endpoint has changed since, the
cache keeps serving the old shape and the suite stays green over an
integration that no longer works.

So: exercise any new model, endpoint or SDK version against the live API
before trusting it, and read a passing unit suite as evidence about this
module's logic only — never about the integration.

Two things here are load-bearing and non-obvious.

**The schema must go through `to_gemini_schema` first.** Pydantic and
Gemini speak different schema dialects: Pydantic emits `$defs`/`$ref` for
nested models, `anyOf: [..., {"type": "null"}]` for optionals, and
`additionalProperties: false` for `extra="forbid"`. Gemini's
`response_schema` rejects the last outright and does not resolve local
refs. Adapting is this module's job precisely so the domain schema under
`core/` can stay strict — only what crosses the wire is narrowed.

**The response must be checked, not assumed.** An earlier version of this
file sent the schema through the Interactions API under a key that API
ignored, so every call came back as unconstrained prose while looking, at
the call site, like structured output. Anything that is not a JSON object
now degrades to ProviderUnavailable rather than reaching a caller that
believes it was constrained.

Every call is paced by a proactive RPM rate limiter, then on a 429 retried
with exponential backoff and jitter up to a configured limit. A 429 that
survives retries, or any other API error, degrades to ProviderUnavailable —
never a crash, never a guess, and the API key is never included in any log
line or exception message.
"""

from __future__ import annotations

import json
import os
import time

import structlog
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel

from llm.provider import MalformedResponse, ProviderUnavailable, TokenUsage
from llm.providers.backoff import sleep_with_backoff

_MODEL_HINTS = ("flash", "flash-lite")

logger = structlog.get_logger(__name__)

# Keys Pydantic emits that Gemini's response_schema either rejects with a
# 400 or silently ignores. `additionalProperties` is the one that actually
# fails the request; the rest are stripped to keep the payload small.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "additionalProperties",
        "$schema",
        "$comment",
        "title",
        "default",
        "discriminator",
        "examples",
        "const",
        "readOnly",
        "writeOnly",
        "patternProperties",
    }
)


def to_gemini_schema(schema: type[BaseModel]) -> dict:
    """Pydantic's JSON Schema rendered into the subset Gemini accepts.

    Inlines local refs, drops keys Gemini rejects, and rewrites nullable
    unions as `nullable: true`. The Pydantic model itself is untouched and
    stays the authority on what a valid response is — this only decides
    what the model is told while generating one.
    """
    document = schema.model_json_schema()
    return _adapt(document, document.get("$defs", {}), frozenset())


def _adapt(node: object, defs: dict, active_refs: frozenset[str]) -> object:
    if isinstance(node, list):
        return [_adapt(item, defs, active_refs) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        target = str(node["$ref"]).rsplit("/", 1)[-1]
        if target in active_refs:
            raise ProviderUnavailable(
                f"schema {target!r} is recursive; Gemini's response_schema cannot express that"
            )
        if target not in defs:
            raise ProviderUnavailable(f"schema reference {node['$ref']!r} cannot be resolved")
        return _adapt(dict(defs[target]), defs, active_refs | {target})

    adapted: dict = {}
    for key, value in node.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS or key == "$defs":
            continue
        if key == "anyOf":
            adapted.update(_adapt_any_of(value, defs, active_refs))
            continue
        adapted[key] = _adapt(value, defs, active_refs)
    return adapted


def _adapt_any_of(variants: list, defs: dict, active_refs: frozenset[str]) -> dict:
    """`int | None` becomes a nullable integer rather than a union carrying a
    null branch, which Gemini does not accept as a variant."""
    concrete = [v for v in variants if not (isinstance(v, dict) and v.get("type") == "null")]
    out: dict = {}
    if len(concrete) < len(variants):
        out["nullable"] = True
    if len(concrete) == 1:
        for key, value in _adapt(concrete[0], defs, active_refs).items():
            out.setdefault(key, value)
    else:
        out["anyOf"] = [_adapt(v, defs, active_refs) for v in concrete]
    return out


class GeminiConfig(BaseModel):
    api_key: str
    model_flash: str = "gemini-3.7-flash"
    model_flash_lite: str = "gemini-flash-lite-latest"
    rpm: int = 15
    max_retries: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> GeminiConfig:
        load_dotenv()
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ProviderUnavailable("GEMINI_API_KEY is not set")
        return cls(
            api_key=api_key,
            model_flash=os.environ.get("GEMINI_MODEL_FLASH", cls.model_fields["model_flash"].default),
            model_flash_lite=os.environ.get(
                "GEMINI_MODEL_FLASH_LITE", cls.model_fields["model_flash_lite"].default
            ),
            # RPM is a Gemini quota, so it keeps the vendor prefix. Retry and
            # backoff are not vendor concepts -- any provider behind the
            # Protocol retries the same way -- so they read the LLM_* names.
            rpm=int(os.environ.get("GEMINI_RPM", cls.model_fields["rpm"].default)),
            max_retries=int(os.environ.get("LLM_MAX_RETRIES", cls.model_fields["max_retries"].default)),
            backoff_base_seconds=float(
                os.environ.get(
                    "LLM_RETRY_BACKOFF_BASE", cls.model_fields["backoff_base_seconds"].default
                )
            ),
        )

    def model_for(self, model_hint: str) -> str:
        if model_hint == "flash":
            return self.model_flash
        if model_hint == "flash-lite":
            return self.model_flash_lite
        raise ValueError(f"unknown model_hint {model_hint!r}; expected one of {_MODEL_HINTS}")


class RateLimiter:
    """Proactively paces calls to roughly `rpm` per minute."""

    def __init__(self, rpm: int):
        self._min_interval = 60.0 / rpm if rpm > 0 else 0.0
        self._last_call: float | None = None

    def wait(self) -> None:
        if self._last_call is not None:
            remaining = self._min_interval - (time.monotonic() - self._last_call)
            if remaining > 0:
                time.sleep(remaining)
        self._last_call = time.monotonic()


class GeminiProvider:
    def __init__(self, config: GeminiConfig | None = None, client=None):
        self._config = config or GeminiConfig.from_env()
        self._client = client or genai.Client(api_key=self._config.api_key)
        self._rate_limiter = RateLimiter(self._config.rpm)
        self.reset_counters()

    def reset_counters(self) -> None:
        """Zero the per-run counters EVIDENCE.md §10 reports. Nothing in the
        audit path calls this; eval/ does, between runs."""
        self.live_calls = 0
        self.rate_limited = 0
        self.backoff_seconds = 0.0
        self.prompt_tokens = 0
        self.response_tokens = 0
        self.total_tokens = 0
        self.last_usage: TokenUsage | None = None

    def generate_structured(self, prompt: str, schema: type[BaseModel], model_hint: str) -> dict:
        model = self._config.model_for(model_hint)
        response_schema = to_gemini_schema(schema)
        last_error: Exception | None = None
        self.last_usage = None

        for attempt in range(self._config.max_retries + 1):
            self._rate_limiter.wait()
            try:
                self.live_calls += 1
                response = self._client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=response_schema,
                    ),
                )
                decoded = self._decode(response, model)
                self._record_usage(response)
                return decoded
            except genai_errors.APIError as e:
                last_error = e
                if e.code != 429:
                    raise ProviderUnavailable(f"Gemini API error {e.code} ({e.status})") from e
                self.rate_limited += 1
                logger.warning("gemini_rate_limited", attempt=attempt, model=model, max_retries=self._config.max_retries)
                if attempt >= self._config.max_retries:
                    break
                self._sleep_with_backoff(attempt)

        raise ProviderUnavailable(
            f"Gemini request still rate-limited after {self._config.max_retries} retries"
        ) from last_error

    def _record_usage(self, response: object) -> None:
        """Read the API's own token accounting off the response.

        Read defensively via getattr: `usage_metadata` is telemetry, and a
        missing or renamed field must cost a number in EVIDENCE.md, never a
        successful audit. When it is absent, `last_usage` stays None and
        CachedProvider falls back to a labelled estimate.
        """
        metadata = getattr(response, "usage_metadata", None)
        if metadata is None:
            return
        prompt_tokens = int(getattr(metadata, "prompt_token_count", 0) or 0)
        response_tokens = int(getattr(metadata, "candidates_token_count", 0) or 0)
        total = int(getattr(metadata, "total_token_count", 0) or 0) or (prompt_tokens + response_tokens)
        usage = TokenUsage(
            prompt_tokens=prompt_tokens,
            response_tokens=response_tokens,
            total_tokens=total,
            estimated=False,
        )
        self.last_usage = usage
        self.prompt_tokens += usage.prompt_tokens
        self.response_tokens += usage.response_tokens
        self.total_tokens += usage.total_tokens

    @staticmethod
    def _decode(response: object, model: str) -> dict:
        """Trust the request was constrained only as far as the response
        proves it. A model that answered in prose has not given us a
        structured response, whatever the request asked for."""
        text = getattr(response, "text", None)
        if not text:
            raise MalformedResponse(f"{model} returned an empty response")
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as e:
            raise MalformedResponse(
                f"{model} returned a response that is not valid JSON despite constrained decoding"
            ) from e
        if not isinstance(decoded, dict):
            raise MalformedResponse(
                f"{model} returned a JSON {type(decoded).__name__}, not an object"
            )
        return decoded

    def _sleep_with_backoff(self, attempt: int) -> None:
        self.backoff_seconds += sleep_with_backoff(
            attempt, self._config.backoff_base_seconds, self._config.backoff_max_seconds
        )
