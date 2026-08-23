"""Gemini implementation of LLMProvider, using the Interactions API's
structured-output mode (response_format + JSON schema) for constrained
decoding — never prompt-and-pray.

Every call is paced by a proactive RPM rate limiter, then on a 429 retried
with exponential backoff and jitter up to a configured limit. A 429 that
survives retries, or any other API error, degrades to ProviderUnavailable —
never a crash, never a guess, and the API key is never included in any log
line or exception message.
"""

from __future__ import annotations

import json
import os
import random
import time

import structlog
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from pydantic import BaseModel

from llm.provider import ProviderUnavailable

_MODEL_HINTS = ("flash", "flash-lite")

logger = structlog.get_logger(__name__)


class GeminiConfig(BaseModel):
    api_key: str
    model_flash: str = "gemini-3.7-flash"
    model_flash_lite: str = "gemini-3.7-flash-lite"
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
            rpm=int(os.environ.get("GEMINI_RPM", cls.model_fields["rpm"].default)),
            max_retries=int(os.environ.get("GEMINI_MAX_RETRIES", cls.model_fields["max_retries"].default)),
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

    def generate_structured(self, prompt: str, schema: type[BaseModel], model_hint: str) -> dict:
        model = self._config.model_for(model_hint)
        last_error: Exception | None = None

        for attempt in range(self._config.max_retries + 1):
            self._rate_limiter.wait()
            try:
                interaction = self._client.interactions.create(
                    model=model,
                    input=prompt,
                    response_format={
                        "type": "text",
                        "mime_type": "application/json",
                        "schema": schema.model_json_schema(),
                    },
                )
                return json.loads(interaction.output_text)
            except genai_errors.APIError as e:
                last_error = e
                if e.code != 429:
                    raise ProviderUnavailable(f"Gemini API error {e.code} ({e.status})") from e
                logger.warning("gemini_rate_limited", attempt=attempt, model=model, max_retries=self._config.max_retries)
                if attempt >= self._config.max_retries:
                    break
                self._sleep_with_backoff(attempt)

        raise ProviderUnavailable(
            f"Gemini request still rate-limited after {self._config.max_retries} retries"
        ) from last_error

    def _sleep_with_backoff(self, attempt: int) -> None:
        delay = min(self._config.backoff_base_seconds * (2**attempt), self._config.backoff_max_seconds)
        jitter = random.uniform(0, delay * 0.25)
        time.sleep(delay + jitter)
