"""Tests for llm/providers/retrying.py.

RetryingProvider retries only MalformedResponse -- an empty/non-JSON/non-
object response body -- with the same bounded backoff+jitter formula
GeminiProvider's own 429 loop uses. A plain ProviderUnavailable (429-
exhaustion, a non-429 API error) must pass straight through untouched, so
the two retry loops never compound into an unpredictable total attempt
count.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from llm.provider import MalformedResponse, ProviderUnavailable
from llm.providers.retrying import RetryConfig, RetryingProvider
from tests.support import RecordingProvider


class DummySchema(BaseModel):
    value: str


def _config(max_retries: int = 5) -> RetryConfig:
    # base=0 keeps the suite fast; the formula itself is exercised in
    # llm/providers/backoff.py and GeminiProvider's own tests.
    return RetryConfig(max_retries=max_retries, backoff_base_seconds=0.0, backoff_max_seconds=0.0)


def test_a_malformed_response_then_a_valid_one_retries_and_succeeds():
    attempts = {"n": 0}

    def flaky(prompt, schema, model_hint):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise MalformedResponse(f"attempt {attempts['n']}: bad response")
        return {"value": "ok"}

    inner = RecordingProvider(flaky)
    provider = RetryingProvider(inner, config=_config())

    result = provider.generate_structured("prompt", DummySchema, "flash")

    assert result == {"value": "ok"}
    assert attempts["n"] == 3
    assert len(inner.calls) == 3


def test_a_persistently_malformed_response_exhausts_retries_and_raises():
    inner = RecordingProvider(MalformedResponse("always bad"))
    provider = RetryingProvider(inner, config=_config(max_retries=3))

    with pytest.raises(ProviderUnavailable, match="malformed response after 3 retries"):
        provider.generate_structured("prompt", DummySchema, "flash")

    assert len(inner.calls) == 4  # 1 initial attempt + 3 retries, never more


def test_it_never_retries_forever_for_a_range_of_configured_limits():
    for max_retries in (0, 1, 4, 10):
        inner = RecordingProvider(MalformedResponse("always bad"))
        provider = RetryingProvider(inner, config=_config(max_retries=max_retries))

        with pytest.raises(ProviderUnavailable):
            provider.generate_structured("prompt", DummySchema, "flash")

        assert len(inner.calls) == max_retries + 1


def test_a_plain_provider_unavailable_passes_through_without_any_retry():
    # A 429-exhaustion or a non-429 API error is GeminiProvider's own,
    # already-considered decision -- not something to blindly retry again
    # for an unrelated reason. Scope is disjoint by exception type.
    inner = RecordingProvider(ProviderUnavailable("rate limited, gave up"))
    provider = RetryingProvider(inner, config=_config())

    with pytest.raises(ProviderUnavailable, match="rate limited, gave up"):
        provider.generate_structured("prompt", DummySchema, "flash")

    assert len(inner.calls) == 1, "a plain ProviderUnavailable must not be retried at all"


def test_a_successful_first_call_never_sleeps_or_retries():
    inner = RecordingProvider({"value": "ok"})
    provider = RetryingProvider(inner, config=_config())

    result = provider.generate_structured("prompt", DummySchema, "flash")

    assert result == {"value": "ok"}
    assert len(inner.calls) == 1


def test_last_usage_reaches_through_to_the_inner_provider():
    class _FakeInner:
        def __init__(self):
            self.last_usage = "sentinel"

        def generate_structured(self, prompt, schema, model_hint):
            return {"value": "ok"}

    inner = _FakeInner()
    provider = RetryingProvider(inner, config=_config())
    provider.generate_structured("prompt", DummySchema, "flash")

    assert provider.last_usage == "sentinel"


def test_retry_config_reads_the_same_env_vars_gemini_config_reads(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RETRIES", "7")
    monkeypatch.setenv("LLM_RETRY_BACKOFF_BASE", "2.5")

    config = RetryConfig.from_env()

    assert config.max_retries == 7
    assert config.backoff_base_seconds == 2.5
