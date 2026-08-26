"""Tests for llm/telemetry.py and the provider counters behind it.

These numbers are EVIDENCE.md §10 -- the section that argues the model
boundary is small, cheap and mostly served from disk. A wrong counter here
does not corrupt an audit, but it does put a false claim in the document
whose entire purpose is not over-claiming, so each counter is pinned
against a provider chain whose behaviour the test controls exactly.

The one property worth stating twice: a token count always knows whether it
was measured or estimated. `TokenUsage.estimated` is not decoration -- the
15 cache entries committed before usage tracking existed can only ever be
estimated, and §10 prints the two differently.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from llm.provider import TokenUsage, estimate_usage
from llm.providers.cached import USAGE_SUFFIX, CachedProvider
from llm.providers.null import NullProvider
from llm.telemetry import ProviderTelemetry, reset, snapshot


class DummySchema(BaseModel):
    value: str


class CountingProvider:
    """A vendor-layer stand-in that reports usage and its own counters."""

    def __init__(self, result: dict, usage: TokenUsage | None = None):
        self._result = result
        self._usage = usage
        self.reset_counters()

    def reset_counters(self) -> None:
        self.live_calls = 0
        self.rate_limited = 0
        self.backoff_seconds = 0.0
        self.last_usage: TokenUsage | None = None

    def generate_structured(self, prompt: str, schema: type[BaseModel], model_hint: str) -> dict:
        self.live_calls += 1
        self.last_usage = self._usage
        return self._result


class UncountedProvider:
    """Satisfies the Protocol and tracks nothing -- the third-party case."""

    def generate_structured(self, prompt: str, schema: type[BaseModel], model_hint: str) -> dict:
        return {"value": "hello"}


# ---------------------------------------------------------------------------
# Cache counters
# ---------------------------------------------------------------------------


def test_cached_provider_counts_hits_and_misses(tmp_path):
    provider = CachedProvider(CountingProvider({"value": "hello"}), cache_dir=tmp_path)

    provider.generate_structured("a", DummySchema, "flash")
    provider.generate_structured("a", DummySchema, "flash")
    provider.generate_structured("b", DummySchema, "flash")

    assert provider.misses == 2
    assert provider.hits == 1


def test_a_measured_usage_is_written_as_a_sidecar_and_read_back_on_replay(tmp_path):
    usage = TokenUsage(prompt_tokens=120, response_tokens=30, total_tokens=150, estimated=False)
    inner = CountingProvider({"value": "hello"}, usage=usage)
    provider = CachedProvider(inner, cache_dir=tmp_path)

    provider.generate_structured("a", DummySchema, "flash")
    sidecars = list(tmp_path.glob(f"*{USAGE_SUFFIX}"))
    assert len(sidecars) == 1
    assert json.loads(sidecars[0].read_text(encoding="utf-8"))["total_tokens"] == 150

    replay = CachedProvider(NullProvider(), cache_dir=tmp_path)
    replay.generate_structured("a", DummySchema, "flash")
    assert replay.last_usage == usage
    assert replay.last_usage.estimated is False
    assert replay.estimated_token_calls == 0
    assert replay.total_tokens == 150


def test_a_cache_entry_with_no_sidecar_falls_back_to_a_labelled_estimate(tmp_path):
    """The committed cache's existing entries are exactly this case."""
    seeding = CachedProvider(UncountedProvider(), cache_dir=tmp_path)
    seeding.generate_structured("a", DummySchema, "flash")
    for sidecar in tmp_path.glob(f"*{USAGE_SUFFIX}"):
        sidecar.unlink()

    replay = CachedProvider(NullProvider(), cache_dir=tmp_path)
    replay.generate_structured("a", DummySchema, "flash")
    assert replay.last_usage.estimated is True
    assert replay.estimated_token_calls == 1
    assert replay.total_tokens > 0


def test_a_provider_that_reports_no_usage_never_writes_a_sidecar(tmp_path):
    provider = CachedProvider(UncountedProvider(), cache_dir=tmp_path)
    provider.generate_structured("a", DummySchema, "flash")
    assert list(tmp_path.glob(f"*{USAGE_SUFFIX}")) == []
    assert provider.last_usage.estimated is True


def test_a_corrupt_sidecar_costs_a_measured_number_never_the_run(tmp_path):
    inner = CountingProvider({"value": "hello"}, usage=TokenUsage(total_tokens=99, estimated=False))
    provider = CachedProvider(inner, cache_dir=tmp_path)
    provider.generate_structured("a", DummySchema, "flash")
    for sidecar in tmp_path.glob(f"*{USAGE_SUFFIX}"):
        sidecar.write_text("{not json", encoding="utf-8")

    replay = CachedProvider(NullProvider(), cache_dir=tmp_path)
    result = replay.generate_structured("a", DummySchema, "flash")
    assert result == {"value": "hello"}
    assert replay.last_usage.estimated is True


def test_estimate_usage_is_labelled_and_proportional_to_length():
    small = estimate_usage("a" * 40, "b" * 40)
    large = estimate_usage("a" * 400, "b" * 400)
    assert small.estimated is True
    assert large.total_tokens > small.total_tokens
    assert small.total_tokens == small.prompt_tokens + small.response_tokens


# ---------------------------------------------------------------------------
# Snapshot across a chain
# ---------------------------------------------------------------------------


def test_snapshot_takes_tokens_from_the_cache_layer_and_live_calls_from_the_vendor_layer(tmp_path):
    usage = TokenUsage(prompt_tokens=10, response_tokens=5, total_tokens=15, estimated=False)
    inner = CountingProvider({"value": "hello"}, usage=usage)
    provider = CachedProvider(inner, cache_dir=tmp_path)

    provider.generate_structured("a", DummySchema, "flash")
    provider.generate_structured("a", DummySchema, "flash")  # hit -- no live call

    telemetry = snapshot(provider)
    assert telemetry.calls == 2
    assert telemetry.cache_hits == 1
    assert telemetry.cache_misses == 1
    assert telemetry.live_calls == 1, "a cache hit must never be counted as reaching the network"
    assert telemetry.total_tokens == 30, "both the miss and the replayed hit report measured tokens"
    assert telemetry.cache_hit_rate_bps == 5_000
    assert telemetry.sources == ["CachedProvider", "CountingProvider"]


def test_snapshot_of_a_provider_that_counts_nothing_is_zeros_not_a_crash():
    telemetry = snapshot(UncountedProvider())
    assert telemetry == ProviderTelemetry(calls=0, sources=["UncountedProvider"])
    assert telemetry.cache_hit_rate_bps == 0


def test_reset_zeroes_every_layer_that_knows_how(tmp_path):
    inner = CountingProvider({"value": "hello"}, usage=TokenUsage(total_tokens=7, estimated=False))
    provider = CachedProvider(inner, cache_dir=tmp_path)
    provider.generate_structured("a", DummySchema, "flash")
    assert snapshot(provider).calls == 1

    reset(provider)
    after = snapshot(provider)
    assert after.calls == 0
    assert after.live_calls == 0
    assert after.total_tokens == 0


def test_merging_snapshots_sums_a_whole_sweep(tmp_path):
    left = ProviderTelemetry(calls=3, cache_hits=2, cache_misses=1, live_calls=1, total_tokens=30, sources=["A"])
    right = ProviderTelemetry(calls=5, cache_hits=5, cache_misses=0, live_calls=0, total_tokens=50, sources=["B"])
    merged = left.merged_with(right)
    assert merged.calls == 8
    assert merged.cache_hits == 7
    assert merged.total_tokens == 80
    assert merged.sources == ["A", "B"]
    assert merged.cache_hit_rate_bps == 8_750


def test_a_call_that_reaches_the_network_and_fails_is_still_counted_as_a_miss(tmp_path):
    """Otherwise a run that made a dozen live attempts, every one of them
    rate-limited past its retries, reports a 100% cache hit rate -- which
    is exactly backwards, and exactly what EVIDENCE.md §10 would print."""
    from llm.provider import ProviderUnavailable

    class FailingProvider:
        def __init__(self):
            self.live_calls = 0

        def generate_structured(self, prompt, schema, model_hint):
            self.live_calls += 1
            raise ProviderUnavailable("rate limited past its retries")

    provider = CachedProvider(FailingProvider(), cache_dir=tmp_path)

    for _ in range(3):
        try:
            provider.generate_structured("a", DummySchema, "flash")
        except ProviderUnavailable:
            pass

    telemetry = snapshot(provider)
    assert telemetry.cache_misses == 3
    assert telemetry.cache_hits == 0
    assert telemetry.cache_hit_rate_bps == 0
    assert telemetry.live_calls == 3


def test_a_failed_call_writes_neither_a_cache_entry_nor_a_sidecar(tmp_path):
    from llm.provider import ProviderUnavailable

    class FailingProvider:
        def generate_structured(self, prompt, schema, model_hint):
            raise ProviderUnavailable("nope")

    provider = CachedProvider(FailingProvider(), cache_dir=tmp_path)
    try:
        provider.generate_structured("a", DummySchema, "flash")
    except ProviderUnavailable:
        pass

    assert list(tmp_path.glob("*.json")) == [], "a failure must never poison the cache"
