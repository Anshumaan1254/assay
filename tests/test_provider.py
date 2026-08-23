"""Tests for the LLMProvider boundary: the disk cache never calls the
network on a hit, the null provider always raises, and Gemini's 429 handling
backs off, retries, and degrades to ProviderUnavailable rather than crashing
or leaking the API key.
"""

from __future__ import annotations

import json

import pytest
from google.genai import errors as genai_errors
from pydantic import BaseModel

from llm.provider import ProviderUnavailable
from llm.providers.cached import CachedProvider
from llm.providers.gemini import GeminiConfig, GeminiProvider
from llm.providers.null import NullProvider


class DummySchema(BaseModel):
    value: str


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeProvider:
    def __init__(self, result: dict):
        self.calls = 0
        self._result = result

    def generate_structured(self, prompt: str, schema: type[BaseModel], model_hint: str) -> dict:
        self.calls += 1
        return self._result


def make_client_error(code: int, status: str = "TEST_STATUS") -> genai_errors.ClientError:
    return genai_errors.ClientError(code, {"error": {"code": code, "message": "boom", "status": status}})


class FakeInteractions:
    """Stands in for client.interactions, driven by a queue of responses/exceptions."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses: list):
        self.interactions = FakeInteractions(responses)


class FakeInteractionResult:
    def __init__(self, output_text: str):
        self.output_text = output_text


def make_config(**overrides) -> GeminiConfig:
    # rpm=0 disables the proactive rate limiter's pacing sleep entirely, so
    # tests that assert on time.sleep() call counts only see backoff sleeps.
    defaults = {"api_key": "fake-key-do-not-log", "rpm": 0, "max_retries": 3, "backoff_base_seconds": 0.0}
    defaults.update(overrides)
    return GeminiConfig(**defaults)


# ---------------------------------------------------------------------------
# CachedProvider
# ---------------------------------------------------------------------------


def test_cache_hit_never_calls_inner(tmp_path):
    inner = FakeProvider({"value": "hello"})
    provider = CachedProvider(inner, cache_dir=tmp_path)

    first = provider.generate_structured("prompt", DummySchema, "flash")
    second = provider.generate_structured("prompt", DummySchema, "flash")

    assert first == second == {"value": "hello"}
    assert inner.calls == 1


def test_cache_miss_calls_inner_and_persists_to_disk(tmp_path):
    inner = FakeProvider({"value": "hello"})
    provider = CachedProvider(inner, cache_dir=tmp_path)

    provider.generate_structured("prompt", DummySchema, "flash")

    cached_files = list(tmp_path.glob("*.json"))
    assert len(cached_files) == 1
    assert json.loads(cached_files[0].read_text()) == {"value": "hello"}


def test_cache_key_differs_by_prompt(tmp_path):
    inner = FakeProvider({"value": "hello"})
    provider = CachedProvider(inner, cache_dir=tmp_path)

    provider.generate_structured("prompt A", DummySchema, "flash")
    provider.generate_structured("prompt B", DummySchema, "flash")

    assert inner.calls == 2
    assert len(list(tmp_path.glob("*.json"))) == 2


# ---------------------------------------------------------------------------
# NullProvider
# ---------------------------------------------------------------------------


def test_null_provider_always_raises():
    with pytest.raises(ProviderUnavailable):
        NullProvider().generate_structured("prompt", DummySchema, "flash")


# ---------------------------------------------------------------------------
# GeminiProvider — config
# ---------------------------------------------------------------------------


def test_missing_api_key_raises_provider_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here, so load_dotenv() finds nothing to load
    with pytest.raises(ProviderUnavailable):
        GeminiConfig.from_env()


def test_unknown_model_hint_raises_value_error():
    config = make_config()
    with pytest.raises(ValueError):
        config.model_for("not-a-real-hint")


# ---------------------------------------------------------------------------
# GeminiProvider — 429 backoff and retry
# ---------------------------------------------------------------------------


def test_429_retries_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    client = FakeClient(
        [
            make_client_error(429),
            make_client_error(429),
            FakeInteractionResult(json.dumps({"value": "ok"})),
        ]
    )
    provider = GeminiProvider(config=make_config(max_retries=3), client=client)

    result = provider.generate_structured("prompt", DummySchema, "flash")

    assert result == {"value": "ok"}
    assert client.interactions.calls == 3
    assert len(sleeps) == 2


def test_429_exhausting_retries_raises_provider_unavailable(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    client = FakeClient([make_client_error(429) for _ in range(4)])
    provider = GeminiProvider(config=make_config(max_retries=3), client=client)

    with pytest.raises(ProviderUnavailable):
        provider.generate_structured("prompt", DummySchema, "flash")

    assert client.interactions.calls == 4  # 1 initial + 3 retries


def test_non_429_api_error_fails_fast_without_retry(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    client = FakeClient([make_client_error(403, "PERMISSION_DENIED")])
    provider = GeminiProvider(config=make_config(max_retries=3), client=client)

    with pytest.raises(ProviderUnavailable):
        provider.generate_structured("prompt", DummySchema, "flash")

    assert client.interactions.calls == 1
    assert sleeps == []


def test_api_key_never_appears_in_exception_message(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    secret = "sk-super-secret-do-not-leak"
    client = FakeClient([make_client_error(429) for _ in range(2)])
    provider = GeminiProvider(config=make_config(api_key=secret, max_retries=1), client=client)

    with pytest.raises(ProviderUnavailable) as exc_info:
        provider.generate_structured("prompt", DummySchema, "flash")

    assert secret not in str(exc_info.value)


def test_api_key_never_logged_on_retry(monkeypatch, caplog):
    monkeypatch.setattr("time.sleep", lambda s: None)
    secret = "sk-super-secret-do-not-leak"
    client = FakeClient([make_client_error(429), FakeInteractionResult(json.dumps({"value": "ok"}))])
    provider = GeminiProvider(config=make_config(api_key=secret, max_retries=1), client=client)

    provider.generate_structured("prompt", DummySchema, "flash")

    assert secret not in caplog.text
