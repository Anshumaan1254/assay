"""Razorpay REST client: authenticated, paginated, rate-limit aware.

Deliberately thin. This module's entire job is to return Razorpay's raw
JSON dicts; it maps nothing into Assay's domain model (that is
`ingest/mapping.py`) and computes no money. Keeping the vendor's wire
format and Assay's domain model separated by an explicit mapping step is
what makes a second gateway a new mapping module rather than a rewrite --
the same "the boundary is an interface, not a vendor" discipline
`llm/provider.py` applies to models.

Three endpoints are used, and each is used for exactly one thing:

  - `/v1/settlements/recon/combined` -- the money endpoint. One row per
    settled transaction, carrying `amount`/`fee`/`tax` as integer paise
    plus the `settlement_id`/`settlement_utr` that ties it to a payout.
    This is what becomes the ledger and the settlement report.
  - `/v1/settlements` -- the payouts themselves, for the settlement
    batches and (see the package docstring's caveat) the stand-in bank
    statement.
  - `/v1/payments` -- joined onto the recon rows by payment id, for the
    two fields the recon report does not carry: `international` and the
    nested `card` object. `ingest/mapping.py` needs `international` to
    decide whether a transaction's contract fee is single-component and
    therefore auditable at all.

**The API key is never logged.** Not in a log line, not in an exception
message, not in a repr. `RazorpayConfig.key_secret` is read from the
environment and passed straight to httpx's auth; every error raised here
names the endpoint and status code only.
"""

from __future__ import annotations

import os
import random
import time
from typing import Any, Self

import httpx
import structlog
from dotenv import load_dotenv
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

BASE_URL = "https://api.razorpay.com/v1"

# Razorpay's own documented ceilings, which differ per endpoint: the recon
# report allows 1000 records per page, settlements and payments only 100.
# Read as constants rather than guessed at each call site.
RECON_MAX_COUNT = 1000
STANDARD_MAX_COUNT = 100

# A page cap, not a record cap: a runaway pagination loop against a live
# API is a failure mode worth bounding explicitly rather than trusting the
# server's own `count` to eventually shrink.
MAX_PAGES = 500


class RazorpayUnavailable(Exception):
    """No usable response from Razorpay -- missing credentials, a network
    failure, a non-2xx status, or a rate limit that outlived its retries.
    Every failure mode funnels through this one type so a caller never has
    to catch httpx's exception hierarchy, and so an API key can never
    reach a traceback by way of an httpx exception repr."""


class RazorpayConfig(BaseModel):
    key_id: str
    key_secret: str
    base_url: str = BASE_URL
    timeout_seconds: float = 30.0
    max_retries: int = 5
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> RazorpayConfig:
        load_dotenv()
        key_id = os.environ.get("RAZORPAY_KEY_ID")
        key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
        if not key_id or not key_secret:
            raise RazorpayUnavailable(
                "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must both be set (see .env)"
            )
        return cls(
            key_id=key_id,
            key_secret=key_secret,
            base_url=os.environ.get("RAZORPAY_BASE_URL", BASE_URL),
            timeout_seconds=float(os.environ.get("RAZORPAY_TIMEOUT_SECONDS", cls.model_fields["timeout_seconds"].default)),
            max_retries=int(os.environ.get("RAZORPAY_MAX_RETRIES", cls.model_fields["max_retries"].default)),
        )

    @property
    def is_test_mode(self) -> bool:
        """Razorpay test keys are prefixed `rzp_test_`; live keys
        `rzp_live_`. Recorded in the run manifest so a run built from test
        data can never be mistaken for a real merchant's money."""
        return self.key_id.startswith("rzp_test_")


class RazorpayClient:
    def __init__(self, config: RazorpayConfig | None = None, client: httpx.Client | None = None):
        self._config = config or RazorpayConfig.from_env()
        self._client = client or httpx.Client(timeout=self._config.timeout_seconds)
        self.request_count = 0

    @property
    def config(self) -> RazorpayConfig:
        return self._config

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- the three endpoints -------------------------------------------

    def fetch_recon(self, year: int, month: int, day: int | None = None) -> list[dict[str, Any]]:
        """Every settled transaction for a month (or one day of it)."""
        params: dict[str, Any] = {"year": year, "month": month}
        if day is not None:
            params["day"] = day
        return self._paginate("/settlements/recon/combined", params, page_size=RECON_MAX_COUNT)

    def fetch_settlements(self, from_ts: int, to_ts: int) -> list[dict[str, Any]]:
        """Every payout in a Unix-timestamp window."""
        return self._paginate(
            "/settlements", {"from": from_ts, "to": to_ts}, page_size=STANDARD_MAX_COUNT
        )

    def fetch_payments(self, from_ts: int, to_ts: int) -> list[dict[str, Any]]:
        """Every payment in a Unix-timestamp window, for the `international`
        and `card` fields the recon report omits."""
        return self._paginate(
            "/payments", {"from": from_ts, "to": to_ts}, page_size=STANDARD_MAX_COUNT
        )

    # -- transport ------------------------------------------------------

    def _paginate(self, path: str, params: dict[str, Any], page_size: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        skip = 0
        for _page in range(MAX_PAGES):
            payload = self._get(path, {**params, "count": page_size, "skip": skip})
            batch = payload.get("items", [])
            if not isinstance(batch, list):
                raise RazorpayUnavailable(f"{path}: expected an 'items' list, got {type(batch).__name__}")
            items.extend(batch)
            if len(batch) < page_size:
                return items
            skip += len(batch)
        raise RazorpayUnavailable(f"{path}: still paginating after {MAX_PAGES} pages; refusing to loop further")

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._config.base_url}{path}"
        last_status: int | None = None

        for attempt in range(self._config.max_retries + 1):
            try:
                self.request_count += 1
                response = self._client.get(
                    url, params=params, auth=(self._config.key_id, self._config.key_secret)
                )
            except httpx.HTTPError as error:
                # Deliberately does not interpolate `error` -- an httpx
                # exception repr can carry the request URL, and the auth
                # tuple is not in the URL but the class of mistake (a
                # credential reaching a traceback) is worth refusing
                # structurally rather than auditing case by case.
                raise RazorpayUnavailable(f"{path}: transport error ({type(error).__name__})") from None

            last_status = response.status_code
            if response.status_code == 200:
                return self._decode(response, path)
            if response.status_code != 429:
                raise RazorpayUnavailable(f"{path}: Razorpay returned HTTP {response.status_code}")

            logger.warning("razorpay_rate_limited", path=path, attempt=attempt, max_retries=self._config.max_retries)
            if attempt >= self._config.max_retries:
                break
            self._sleep_with_backoff(attempt)

        raise RazorpayUnavailable(
            f"{path}: still rate-limited (HTTP {last_status}) after {self._config.max_retries} retries"
        )

    @staticmethod
    def _decode(response: httpx.Response, path: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise RazorpayUnavailable(f"{path}: response body was not valid JSON") from error
        if not isinstance(payload, dict):
            raise RazorpayUnavailable(f"{path}: expected a JSON object, got {type(payload).__name__}")
        return payload

    def _sleep_with_backoff(self, attempt: int) -> None:
        # Local rather than reusing llm/providers/backoff.py: that helper
        # is generic, but it lives under llm/ and a vendor REST client
        # reaching into the model-boundary package for a sleep function
        # would be a worse coupling than ten lines here. Different failure
        # domain, different config, different retry budget.
        delay = min(self._config.backoff_base_seconds * (2**attempt), self._config.backoff_max_seconds)
        time.sleep(delay + random.uniform(0, delay * 0.25))
