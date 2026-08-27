"""Exponential backoff with jitter, shared by every retry loop in llm/.

Extracted out of GeminiProvider (the 429-retry loop) so RetryingProvider's
malformed-response retry loop uses the exact same formula rather than a
second, independently-drifting one. Vendor-agnostic on purpose: retry and
backoff are not vendor concepts (only the RPM limit is), so both loops read
the same LLM_MAX_RETRIES / LLM_RETRY_BACKOFF_BASE env vars.
"""

from __future__ import annotations

import random
import time


def sleep_with_backoff(attempt: int, base_seconds: float, max_seconds: float) -> float:
    """Sleep `min(base * 2**attempt, max) + up to 25% jitter`, and return how
    long that was, so a caller that tracks a `backoff_seconds` counter (for
    EVIDENCE.md) can accumulate it."""
    delay = min(base_seconds * (2**attempt), max_seconds)
    jitter = random.uniform(0, delay * 0.25)
    total = delay + jitter
    time.sleep(total)
    return total
