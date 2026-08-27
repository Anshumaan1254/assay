"""C09b -- rate limited (429 on every call, then recovery).

GeminiProvider paces calls, then retries a 429 with exponential
backoff+jitter up to a configured limit before degrading to
ProviderUnavailable -- never a crash, never an infinite retry. This proves
both directions: a 429 storm that recovers before max_retries is fully
absorbed and the call succeeds; a sustained one degrades cleanly, and the
total number of attempts (and therefore the worst-case real wall clock,
bounded by max_retries * backoff_max_seconds) is finite and small -- a
retry storm that takes an hour is a failure this test would catch via its
own tight timeout.

time.sleep is mocked so this test itself runs in milliseconds; what's
under test is the retry-count logic, not real elapsed time.
"""

from __future__ import annotations

import pytest
from google.genai import errors as genai_errors
from pydantic import BaseModel

from chaos.incident import chaos_scenario
from llm.provider import ProviderUnavailable
from llm.providers.gemini import GeminiConfig, GeminiProvider


class DummySchema(BaseModel):
    value: str


def _rate_limit_error() -> genai_errors.ClientError:
    return genai_errors.ClientError(429, {"error": {"code": 429, "message": "rate limited", "status": "RESOURCE_EXHAUSTED"}})


class _FakeModels:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls = 0

    def generate_content(self, **kwargs):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeClient:
    def __init__(self, responses: list) -> None:
        self.models = _FakeModels(responses)


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.text = text


@pytest.mark.timeout(10)
def test_c09b_a_429_storm_that_recovers_before_the_limit_is_fully_absorbed(monkeypatch):
    monkeypatch.setattr("llm.providers.backoff.time.sleep", lambda s: None)

    with chaos_scenario(
        "C09b",
        title="Rate limited, then recovery",
        category="degraded_gracefully",
        failure_injected="429 on the first 3 calls, success on the 4th",
        expected_behavior="backoff+jitter absorbs it within the configured retry budget; the call succeeds",
    ) as scenario:
        responses = [_rate_limit_error(), _rate_limit_error(), _rate_limit_error(), _FakeResult('{"value": "ok"}')]
        config = GeminiConfig(api_key="fake-key", rpm=0, max_retries=5, backoff_base_seconds=1.0, backoff_max_seconds=8.0)
        provider = GeminiProvider(config=config, client=_FakeClient(responses))

        result = provider.generate_structured("prompt", DummySchema, "flash")

        assert result == {"value": "ok"}
        assert provider.rate_limited == 3
        scenario.note(f"absorbed {provider.rate_limited} rate-limit hits, then succeeded")


@pytest.mark.timeout(10)
def test_c09b_a_sustained_429_degrades_cleanly_with_a_bounded_number_of_attempts(monkeypatch):
    monkeypatch.setattr("llm.providers.backoff.time.sleep", lambda s: None)

    with chaos_scenario(
        "C09b-sustained",
        title="Sustained rate limiting",
        category="degraded_gracefully",
        failure_injected="every single call returns 429, without exception",
        expected_behavior="degrades to ProviderUnavailable after exactly max_retries+1 attempts, never a crash",
    ) as scenario:
        max_retries = 4
        backoff_max_seconds = 8.0
        responses = [_rate_limit_error() for _ in range(max_retries + 1)]
        config = GeminiConfig(
            api_key="fake-key", rpm=0, max_retries=max_retries,
            backoff_base_seconds=1.0, backoff_max_seconds=backoff_max_seconds,
        )
        provider = GeminiProvider(config=config, client=_FakeClient(responses))

        with pytest.raises(ProviderUnavailable):
            provider.generate_structured("prompt", DummySchema, "flash")

        assert provider.rate_limited == max_retries + 1, "every attempt, including the first, was rate-limited"
        assert provider.live_calls == max_retries + 1, "never more than one initial attempt plus max_retries"
        worst_case_wall_clock_seconds = max_retries * backoff_max_seconds * 1.25  # +25% for jitter's ceiling
        assert provider.backoff_seconds <= worst_case_wall_clock_seconds, "a retry storm that takes an hour is also a failure"

        scenario.note(
            f"gave up after {provider.live_calls} calls; worst-case bound "
            f"{worst_case_wall_clock_seconds:.1f}s, actual backoff {provider.backoff_seconds:.2f}s"
        )
        scenario.money_impact_paise = None
