"""Tests for the LLMProvider boundary: the disk cache never calls the
network on a hit, the null provider always raises, and Gemini's 429 handling
backs off, retries, and degrades to ProviderUnavailable rather than crashing
or leaking the API key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from google.genai import errors as genai_errors
from pydantic import BaseModel

from llm.provider import ProviderUnavailable
from llm.providers.cached import CachedProvider, default_cache_dir
from llm.providers.gemini import GeminiConfig, GeminiProvider, to_gemini_schema
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


class FakeModels:
    """Stands in for client.models, driven by a queue of responses/exceptions."""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0
        self.last_kwargs: dict = {}

    def generate_content(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses: list):
        self.models = FakeModels(responses)


class FakeGenerateResult:
    def __init__(self, text: str | None):
        self.text = text


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


def test_a_truncated_cache_entry_degrades_to_a_refetch_not_a_crash(tmp_path):
    # The shape of a process killed mid-write to the committed .llm_cache/:
    # the file exists but its content is torn. Must cost one re-fetch,
    # never a raw JSONDecodeError leaking out of a cache hit.
    inner = FakeProvider({"value": "fresh"})
    provider = CachedProvider(inner, cache_dir=tmp_path)
    provider.generate_structured("prompt", DummySchema, "flash")
    assert inner.calls == 1

    cache_file = next(tmp_path.glob("*.json"))
    cache_file.write_text('{"value": "fre', encoding="utf-8")  # torn mid-write

    result = provider.generate_structured("prompt", DummySchema, "flash")

    assert result == {"value": "fresh"}
    assert inner.calls == 2, "a corrupt entry must be treated as a miss, re-fetching from the inner provider"
    assert json.loads(cache_file.read_text()) == {"value": "fresh"}, "the cache should self-heal on refetch"


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


def test_missing_api_key_raises_provider_unavailable(monkeypatch):
    # load_dotenv() resolves its default path from the *calling module's*
    # location, not the cwd, so chdir'ing to a tmp_path does not stop it
    # finding the repo's own .env -- this test passed only while that file
    # happened to be empty. Stub the load out so this asserts the config
    # logic rather than the developer's filesystem.
    monkeypatch.setattr("llm.providers.gemini.load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

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
            FakeGenerateResult(json.dumps({"value": "ok"})),
        ]
    )
    provider = GeminiProvider(config=make_config(max_retries=3), client=client)

    result = provider.generate_structured("prompt", DummySchema, "flash")

    assert result == {"value": "ok"}
    assert client.models.calls == 3
    assert len(sleeps) == 2


def test_429_exhausting_retries_raises_provider_unavailable(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)

    client = FakeClient([make_client_error(429) for _ in range(4)])
    provider = GeminiProvider(config=make_config(max_retries=3), client=client)

    with pytest.raises(ProviderUnavailable):
        provider.generate_structured("prompt", DummySchema, "flash")

    assert client.models.calls == 4  # 1 initial + 3 retries


def test_non_429_api_error_fails_fast_without_retry(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleeps.append(s))

    client = FakeClient([make_client_error(403, "PERMISSION_DENIED")])
    provider = GeminiProvider(config=make_config(max_retries=3), client=client)

    with pytest.raises(ProviderUnavailable):
        provider.generate_structured("prompt", DummySchema, "flash")

    assert client.models.calls == 1
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
    client = FakeClient([make_client_error(429), FakeGenerateResult(json.dumps({"value": "ok"}))])
    provider = GeminiProvider(config=make_config(api_key=secret, max_retries=1), client=client)

    provider.generate_structured("prompt", DummySchema, "flash")

    assert secret not in caplog.text


# ---------------------------------------------------------------------------
# GeminiProvider — constrained decoding actually reaches the API
# ---------------------------------------------------------------------------


def test_the_schema_is_sent_as_a_response_schema():
    """The bug this guards against was silent: the schema was passed under a
    key the SDK ignored, so every call was unconstrained prose while looking
    like structured output. Assert the schema reaches the request."""
    client = FakeClient([FakeGenerateResult(json.dumps({"value": "ok"}))])
    provider = GeminiProvider(config=make_config(), client=client)

    provider.generate_structured("prompt", DummySchema, "flash")

    config = client.models.last_kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is not None
    assert "value" in config.response_schema["properties"]
    assert client.models.last_kwargs["contents"] == "prompt"


def test_an_empty_response_degrades_to_provider_unavailable():
    client = FakeClient([FakeGenerateResult(None)])
    provider = GeminiProvider(config=make_config(), client=client)

    with pytest.raises(ProviderUnavailable, match="empty"):
        provider.generate_structured("prompt", DummySchema, "flash")


def test_a_non_json_response_degrades_to_provider_unavailable():
    """Never a crash and never a guess -- if constrained decoding failed to
    constrain, that is a provider failure, not a parsing puzzle."""
    client = FakeClient([FakeGenerateResult("Here are three payment methods: ...")])
    provider = GeminiProvider(config=make_config(), client=client)

    with pytest.raises(ProviderUnavailable, match="not valid JSON"):
        provider.generate_structured("prompt", DummySchema, "flash")


def test_a_json_array_response_degrades_to_provider_unavailable():
    client = FakeClient([FakeGenerateResult(json.dumps(["not", "an", "object"]))])
    provider = GeminiProvider(config=make_config(), client=client)

    with pytest.raises(ProviderUnavailable):
        provider.generate_structured("prompt", DummySchema, "flash")


# ---------------------------------------------------------------------------
# to_gemini_schema — Pydantic's dialect is not Gemini's
# ---------------------------------------------------------------------------


class Nested(BaseModel):
    count: int
    label: str | None = None


class Outer(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    child: Nested
    children: list[Nested]
    cap: int | None = None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def test_additional_properties_is_stripped():
    """extra="forbid" on our domain models emits additionalProperties, which
    Gemini rejects outright with a 400."""
    schema = to_gemini_schema(Outer)

    assert all("additionalProperties" not in node for node in _walk(schema))


def test_refs_are_inlined():
    """Gemini does not resolve local $ref, so nested models must be expanded."""
    schema = to_gemini_schema(Outer)

    assert all("$ref" not in node and "$defs" not in node for node in _walk(schema))
    assert schema["properties"]["child"]["properties"]["count"]["type"] == "integer"
    assert schema["properties"]["children"]["items"]["properties"]["count"]["type"] == "integer"


def test_optionals_become_nullable_rather_than_a_null_union():
    schema = to_gemini_schema(Outer)
    cap = schema["properties"]["cap"]

    assert cap["nullable"] is True
    assert cap["type"] == "integer"
    assert "anyOf" not in cap


def test_enums_and_required_survive():
    from core.contract import RateCardParse

    schema = to_gemini_schema(RateCardParse)

    assert "merchant_id" in schema["required"]
    fee_types = schema["properties"]["rules"]["items"]["properties"]["fee_type"]
    assert "mdr" in fee_types["enum"]


def test_the_real_contract_schema_carries_nothing_gemini_rejects():
    from core.contract import RateCardParse

    schema = to_gemini_schema(RateCardParse)

    for node in _walk(schema):
        assert not any(key.startswith("$") for key in node), node
        assert "additionalProperties" not in node


def test_a_recursive_schema_is_refused_rather_than_looping():
    class Node(BaseModel):
        child: Node | None = None

    Node.model_rebuild()

    with pytest.raises(ProviderUnavailable, match="recursive"):
        to_gemini_schema(Node)


# ---------------------------------------------------------------------------
# Configuration actually reaches the code that uses it
# ---------------------------------------------------------------------------


def test_flash_lite_default_is_a_model_that_exists():
    """gemini-3.7-flash-lite is not a real model on the free tier; the
    narrator would have failed on its first call."""
    assert GeminiConfig.model_fields["model_flash_lite"].default == "gemini-flash-lite-latest"


def test_from_env_reads_llm_max_retries(monkeypatch):
    monkeypatch.setattr("llm.providers.gemini.load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-do-not-log")
    monkeypatch.setenv("LLM_MAX_RETRIES", "7")

    assert GeminiConfig.from_env().max_retries == 7


def test_from_env_reads_llm_retry_backoff_base(monkeypatch):
    monkeypatch.setattr("llm.providers.gemini.load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-do-not-log")
    monkeypatch.setenv("LLM_RETRY_BACKOFF_BASE", "2.5")

    assert GeminiConfig.from_env().backoff_base_seconds == 2.5


def test_from_env_falls_back_to_defaults_when_unset(monkeypatch):
    monkeypatch.setattr("llm.providers.gemini.load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-do-not-log")
    for name in ("LLM_MAX_RETRIES", "LLM_RETRY_BACKOFF_BASE", "GEMINI_RPM"):
        monkeypatch.delenv(name, raising=False)

    config = GeminiConfig.from_env()

    assert config.max_retries == GeminiConfig.model_fields["max_retries"].default
    assert config.backoff_base_seconds == GeminiConfig.model_fields["backoff_base_seconds"].default
    assert config.rpm == GeminiConfig.model_fields["rpm"].default


def test_default_cache_dir_is_llm_cache(monkeypatch):
    monkeypatch.setattr("llm.providers.cached.load_dotenv", lambda *a, **k: False)
    monkeypatch.delenv("LLM_CACHE_DIR", raising=False)

    assert default_cache_dir() == Path(".llm_cache")


def test_default_cache_dir_honours_llm_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("llm.providers.cached.load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "elsewhere"))

    assert default_cache_dir() == tmp_path / "elsewhere"


def test_cached_provider_uses_llm_cache_dir_when_none_given(monkeypatch, tmp_path):
    monkeypatch.setattr("llm.providers.cached.load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "from-env"))
    inner = FakeProvider({"value": "hello"})

    CachedProvider(inner).generate_structured("prompt", DummySchema, "flash")

    assert len(list((tmp_path / "from-env").glob("*.json"))) == 1


def test_an_explicit_cache_dir_still_wins_over_the_environment(monkeypatch, tmp_path):
    monkeypatch.setattr("llm.providers.cached.load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("LLM_CACHE_DIR", str(tmp_path / "ignored"))
    inner = FakeProvider({"value": "hello"})

    CachedProvider(inner, cache_dir=tmp_path / "explicit").generate_structured(
        "prompt", DummySchema, "flash"
    )

    assert len(list((tmp_path / "explicit").glob("*.json"))) == 1
    assert not (tmp_path / "ignored").exists()


# ---------------------------------------------------------------------------
# Gemini telemetry counters (EVIDENCE.md §10)
#
# These are observations, not control flow: a counter must never change what
# a call returns, and a missing usage_metadata field must cost a number in
# the report rather than a working audit.
# ---------------------------------------------------------------------------


class FakeUsage:
    def __init__(self, prompt: int, candidates: int, total: int | None = None):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.total_token_count = total if total is not None else prompt + candidates


class FakeGenerateResultWithUsage(FakeGenerateResult):
    def __init__(self, text: str | None, usage=None):
        super().__init__(text)
        self.usage_metadata = usage


def test_gemini_records_the_apis_own_token_accounting():
    client = FakeClient([FakeGenerateResultWithUsage('{"value": "x"}', FakeUsage(100, 25))])
    provider = GeminiProvider(config=make_config(), client=client)

    provider.generate_structured("prompt", DummySchema, "flash")

    assert provider.last_usage.prompt_tokens == 100
    assert provider.last_usage.response_tokens == 25
    assert provider.last_usage.total_tokens == 125
    assert provider.last_usage.estimated is False
    assert provider.live_calls == 1


def test_a_response_without_usage_metadata_still_succeeds_and_reports_no_usage():
    client = FakeClient([FakeGenerateResultWithUsage('{"value": "x"}', None)])
    provider = GeminiProvider(config=make_config(), client=client)

    assert provider.generate_structured("prompt", DummySchema, "flash") == {"value": "x"}
    assert provider.last_usage is None
    assert provider.total_tokens == 0


def test_gemini_counts_every_429_and_the_time_spent_backing_off(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("llm.providers.backoff.time.sleep", slept.append)
    monkeypatch.setattr("llm.providers.backoff.random.uniform", lambda a, b: 0.0)

    responses = [make_client_error(429), make_client_error(429), FakeGenerateResultWithUsage('{"value": "x"}')]
    provider = GeminiProvider(config=make_config(backoff_base_seconds=1.0), client=FakeClient(responses))

    provider.generate_structured("prompt", DummySchema, "flash")

    assert provider.rate_limited == 2
    assert provider.live_calls == 3, "each attempt is a call that reached the network"
    assert provider.backoff_seconds == sum(slept) == 3.0  # 1s + 2s


def test_reset_counters_clears_gemini_telemetry():
    client = FakeClient([FakeGenerateResultWithUsage('{"value": "x"}', FakeUsage(10, 5))])
    provider = GeminiProvider(config=make_config(), client=client)
    provider.generate_structured("prompt", DummySchema, "flash")

    provider.reset_counters()

    assert provider.live_calls == 0
    assert provider.total_tokens == 0
    assert provider.last_usage is None
