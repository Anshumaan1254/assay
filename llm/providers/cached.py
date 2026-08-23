"""Disk-cache wrapper around any LLMProvider.

The cache directory is committed to the repo (see .llm_cache/), so `make
demo` and replays never need a live API key or network access — a cache hit
never calls the wrapped provider.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel

DEFAULT_CACHE_DIR = Path(".llm_cache")


class CachedProvider:
    def __init__(self, inner, cache_dir: Path = DEFAULT_CACHE_DIR):
        self._inner = inner
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def generate_structured(self, prompt: str, schema: type[BaseModel], model_hint: str) -> dict:
        cache_file = self._cache_dir / f"{self._cache_key(prompt, schema, model_hint)}.json"
        if cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

        result = self._inner.generate_structured(prompt, schema, model_hint)
        cache_file.write_text(json.dumps(result, sort_keys=True, indent=2), encoding="utf-8")
        return result

    @staticmethod
    def _cache_key(prompt: str, schema: type[BaseModel], model_hint: str) -> str:
        schema_repr = json.dumps(schema.model_json_schema(), sort_keys=True)
        raw = f"{prompt}\x00{schema_repr}\x00{model_hint}".encode()
        return hashlib.sha256(raw).hexdigest()
