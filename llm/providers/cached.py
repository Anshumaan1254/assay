"""Disk-cache wrapper around any LLMProvider.

The cache directory is committed to the repo (see .llm_cache/), so `make
demo` and replays never need a live API key or network access — a cache hit
never calls the wrapped provider.

Where the cache lives is read from LLM_CACHE_DIR, not hard-coded — the
same rule the rate limits follow. It is deliberately not named for a
vendor: caching a prompt hash is not a Gemini concern, and this wrapper
sits over any provider.

**Usage sidecars.** EVIDENCE.md has to report tokens per 1,000 records and
what that would cost at paid-tier rates, and a replay off this cache never
contacts a provider that could say. So on a miss, if the wrapped provider
reported its own usage, that usage is written next to the response as
`<hash>.usage.json`. On a hit the sidecar is read back, and a replay
reports the *measured* tokens from the call that populated it.

The cache key is unchanged by this. Sidecars are a strictly additive file
alongside an existing entry, so every response already committed stays
valid; those entries simply have no sidecar until something re-fetches
them, and a hit on one falls back to a byte-length estimate that is
labelled `estimated` all the way out to the report.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import structlog
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from llm.provider import TokenUsage, estimate_usage

FALLBACK_CACHE_DIR = Path(".llm_cache")
USAGE_SUFFIX = ".usage.json"

logger = structlog.get_logger(__name__)


def default_cache_dir() -> Path:
    """Resolved per call rather than at import, so a test or a chaos
    scenario can redirect the cache without reloading the module."""
    load_dotenv()
    return Path(os.environ.get("LLM_CACHE_DIR") or FALLBACK_CACHE_DIR)


class CachedProvider:
    def __init__(self, inner, cache_dir: Path | str | None = None):
        self._inner = inner
        self._cache_dir = Path(cache_dir) if cache_dir is not None else default_cache_dir()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self.reset_counters()

    def reset_counters(self) -> None:
        """Zero the per-run counters. eval/ measures one audit at a time and
        needs a clean slate between them; nothing in the audit path calls
        this, so a long-lived provider accumulates normally."""
        self.hits = 0
        self.misses = 0
        self.prompt_tokens = 0
        self.response_tokens = 0
        self.total_tokens = 0
        self.estimated_token_calls = 0
        self.last_usage: TokenUsage | None = None

    def generate_structured(self, prompt: str, schema: type[BaseModel], model_hint: str) -> dict:
        key = self._cache_key(prompt, schema, model_hint)
        cache_file = self._cache_dir / f"{key}.json"
        if cache_file.exists():
            payload = cache_file.read_text(encoding="utf-8")
            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError:
                # A truncated/corrupted cache entry -- most plausibly a
                # process killed mid-write to it -- costs one re-fetch,
                # never a crash. The committed .llm_cache/ is meant to be
                # replayable forever; a torn file there must not become a
                # landmine for the next run that happens to hit it.
                logger.warning("corrupt_cache_entry_refetching", key=key)
            else:
                self.hits += 1
                self._record(self._usage_for_hit(key, prompt, payload))
                return decoded

        # Counted before the call, not after. A request that reaches the
        # network and then fails -- a 429 that outlives its retries, say --
        # is still a cache miss, and counting it only on success would
        # report a 100% hit rate for a run that made a dozen live attempts.
        # The counter answers "was this served from disk", which is decided
        # here, not by whether the provider then succeeded.
        self.misses += 1
        result = self._inner.generate_structured(prompt, schema, model_hint)
        payload = json.dumps(result, sort_keys=True, indent=2)
        cache_file.write_text(payload, encoding="utf-8")

        usage = getattr(self._inner, "last_usage", None)
        if usage is None:
            usage = estimate_usage(prompt, payload)
        else:
            self._write_usage(key, usage)
        self._record(usage)
        return result

    def _record(self, usage: TokenUsage) -> None:
        self.last_usage = usage
        self.prompt_tokens += usage.prompt_tokens
        self.response_tokens += usage.response_tokens
        self.total_tokens += usage.total_tokens
        if usage.estimated:
            self.estimated_token_calls += 1

    def _usage_for_hit(self, key: str, prompt: str, payload: str) -> TokenUsage:
        recorded = self._read_usage(key)
        return recorded if recorded is not None else estimate_usage(prompt, payload)

    def _usage_path(self, key: str) -> Path:
        return self._cache_dir / f"{key}{USAGE_SUFFIX}"

    def _read_usage(self, key: str) -> TokenUsage | None:
        path = self._usage_path(key)
        if not path.is_file():
            return None
        try:
            return TokenUsage.model_validate_json(path.read_text(encoding="utf-8"))
        except (ValidationError, json.JSONDecodeError):
            # A corrupt sidecar costs a measured number, never a run. The
            # estimate takes over and says so.
            return None

    def _write_usage(self, key: str, usage: TokenUsage) -> None:
        self._usage_path(key).write_text(usage.model_dump_json(indent=2), encoding="utf-8")

    @staticmethod
    def _cache_key(prompt: str, schema: type[BaseModel], model_hint: str) -> str:
        schema_repr = json.dumps(schema.model_json_schema(), sort_keys=True)
        raw = f"{prompt}\x00{schema_repr}\x00{model_hint}".encode()
        return hashlib.sha256(raw).hexdigest()
