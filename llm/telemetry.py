"""What the model boundary actually cost, read off a provider chain.

EVIDENCE.md §10 is the AI-judgment argument in numbers: how few records
touch a model at all, how many calls that is per 1,000 records, how often
the schema and the reference checker rejected what came back, what it
would cost at paid-tier rates, and how much of a replay is served from
disk. Those numbers have to come from somewhere, and the somewhere is a
set of plain counters on the providers themselves.

Counters are read with `getattr`, never by isinstance-ing a concrete
provider class. `llm/provider.py`'s `LLMProvider` is a one-method Protocol
on purpose -- "the model boundary is an interface, not a vendor" -- and a
third-party provider that tracks nothing must still satisfy it. What that
costs is a telemetry snapshot with zeros in it, which is the right answer:
this module reports what a chain was able to observe, and never invents a
number for a layer that does not keep one.

A chain is walked through `_inner`, outermost first, and each field is
taken from the first layer that reports it. That ordering matters. Token
totals come from `CachedProvider` because it sees hits AND misses; live
call counts, 429s and backoff come from the vendor layer beneath it, which
is the only layer that knows what actually reached the network.
"""

from __future__ import annotations

from pydantic import BaseModel

# (snapshot field, provider attribute). Same name on both sides everywhere
# except `calls`, which is derived below from hits + misses.
_COUNTER_FIELDS = (
    "hits",
    "misses",
    "live_calls",
    "rate_limited",
    "backoff_seconds",
    "prompt_tokens",
    "response_tokens",
    "total_tokens",
    "estimated_token_calls",
)


class ProviderTelemetry(BaseModel):
    """One run's model-boundary cost. Every field is an observation; a zero
    means either "it did not happen" or "no layer of this chain counts
    it", and `sources` says which layers were available to be asked."""

    calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    live_calls: int = 0
    rate_limited: int = 0
    backoff_seconds: float = 0.0
    prompt_tokens: int = 0
    response_tokens: int = 0
    total_tokens: int = 0
    estimated_token_calls: int = 0
    sources: list[str] = []

    @property
    def cache_hit_rate_bps(self) -> int:
        if self.calls == 0:
            return 0
        return 10_000 * self.cache_hits // self.calls

    def merged_with(self, other: ProviderTelemetry) -> ProviderTelemetry:
        """Sum two snapshots. eval/ accumulates one per audit across a
        whole sweep, and a sweep's totals are what §10 reports."""
        return ProviderTelemetry(
            calls=self.calls + other.calls,
            cache_hits=self.cache_hits + other.cache_hits,
            cache_misses=self.cache_misses + other.cache_misses,
            live_calls=self.live_calls + other.live_calls,
            rate_limited=self.rate_limited + other.rate_limited,
            backoff_seconds=self.backoff_seconds + other.backoff_seconds,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            response_tokens=self.response_tokens + other.response_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            estimated_token_calls=self.estimated_token_calls + other.estimated_token_calls,
            sources=sorted(set(self.sources) | set(other.sources)),
        )


def _chain(provider: object) -> list[object]:
    layers: list[object] = []
    seen: set[int] = set()
    current = provider
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        layers.append(current)
        current = getattr(current, "_inner", None)
    return layers


def snapshot(provider: object) -> ProviderTelemetry:
    """Read every counter this provider chain exposes.

    First layer to report a field wins, so wrapping order is what decides
    whose number is authoritative -- see the module docstring.
    """
    found: dict[str, object] = {}
    sources: list[str] = []
    for layer in _chain(provider):
        sources.append(type(layer).__name__)
        for field in _COUNTER_FIELDS:
            if field in found:
                continue
            value = getattr(layer, field, None)
            if value is not None:
                found[field] = value

    hits = int(found.get("hits", 0))
    misses = int(found.get("misses", 0))
    return ProviderTelemetry(
        calls=hits + misses,
        cache_hits=hits,
        cache_misses=misses,
        live_calls=int(found.get("live_calls", 0)),
        rate_limited=int(found.get("rate_limited", 0)),
        backoff_seconds=float(found.get("backoff_seconds", 0.0)),
        prompt_tokens=int(found.get("prompt_tokens", 0)),
        response_tokens=int(found.get("response_tokens", 0)),
        total_tokens=int(found.get("total_tokens", 0)),
        estimated_token_calls=int(found.get("estimated_token_calls", 0)),
        sources=sources,
    )


def reset(provider: object) -> None:
    """Zero every layer that knows how. Called between audits in a sweep so
    each run's telemetry describes only that run."""
    for layer in _chain(provider):
        resetter = getattr(layer, "reset_counters", None)
        if callable(resetter):
            resetter()
