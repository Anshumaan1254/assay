"""Tests for ingest/razorpay.py -- the REST client.

No network: every test drives the real client through an
`httpx.MockTransport`, so pagination, retry and error handling are
exercised against real httpx request/response objects rather than a
hand-rolled fake that could drift from how httpx actually behaves.

The credential tests are the ones worth keeping honest. An API key that
reaches a log line or a traceback is a real incident, and the cheapest
guard against it is a test that asserts the secret appears in neither.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from ingest.razorpay import (
    MAX_PAGES,
    RazorpayClient,
    RazorpayConfig,
    RazorpayUnavailable,
)

KEY_ID = "rzp_test_keyid123"
KEY_SECRET = "supersecret-never-log-me"


def _config(**overrides) -> RazorpayConfig:
    defaults = {
        "key_id": KEY_ID,
        "key_secret": KEY_SECRET,
        "max_retries": 2,
        "backoff_base_seconds": 0.0,
        "backoff_max_seconds": 0.0,
    }
    defaults.update(overrides)
    return RazorpayConfig(**defaults)


def _client(handler, **config_overrides) -> RazorpayClient:
    transport = httpx.MockTransport(handler)
    return RazorpayClient(config=_config(**config_overrides), client=httpx.Client(transport=transport))


def _ok(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"entity": "collection", "count": len(items), "items": items})


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_requests_carry_http_basic_auth_built_from_the_configured_key():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return _ok([])

    _client(handler).fetch_settlements(0, 1)

    expected = base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()
    assert seen == [f"Basic {expected}"]


def test_a_missing_credential_is_a_named_failure_not_a_crash(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    monkeypatch.setattr("ingest.razorpay.load_dotenv", lambda *a, **k: None)

    with pytest.raises(RazorpayUnavailable, match="RAZORPAY_KEY_ID"):
        RazorpayConfig.from_env()


def test_the_key_secret_never_appears_in_an_error_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"description": "forbidden"}})

    with pytest.raises(RazorpayUnavailable) as caught:
        _client(handler).fetch_settlements(0, 1)

    assert KEY_SECRET not in str(caught.value)
    assert KEY_SECRET not in repr(caught.value)


def test_the_key_secret_never_appears_in_a_transport_error_message():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(RazorpayUnavailable) as caught:
        _client(handler).fetch_settlements(0, 1)

    assert KEY_SECRET not in str(caught.value)
    assert "ConnectError" in str(caught.value)


def test_test_mode_is_detected_from_the_key_prefix():
    assert _config(key_id="rzp_test_abc").is_test_mode is True
    assert _config(key_id="rzp_live_abc").is_test_mode is False


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_a_short_page_ends_pagination():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return _ok([{"id": "a"}])

    items = _client(handler).fetch_settlements(0, 1)

    assert items == [{"id": "a"}]
    assert len(calls) == 1, "a page shorter than count means there is nothing more to ask for"


def test_full_pages_are_followed_until_a_short_one():
    pages = [[{"id": str(i)} for i in range(100)], [{"id": str(i)} for i in range(100, 150)]]
    skips: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        skips.append(request.url.params["skip"])
        return _ok(pages[len(skips) - 1])

    items = _client(handler).fetch_settlements(0, 1)

    assert len(items) == 150
    assert skips == ["0", "100"]


def test_the_recon_endpoint_uses_its_own_larger_page_size():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["count"])
        return _ok([])

    _client(handler).fetch_recon(2026, 7)

    assert seen == ["1000"], "recon allows 1000 per page where settlements/payments allow 100"


def test_recon_passes_year_month_and_optional_day():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return _ok([])

    client = _client(handler)
    client.fetch_recon(2026, 7)
    client.fetch_recon(2026, 7, day=15)

    assert seen[0]["year"] == "2026" and seen[0]["month"] == "7"
    assert "day" not in seen[0]
    assert seen[1]["day"] == "15"


def test_pagination_refuses_to_loop_forever():
    # A server that always returns a full page would otherwise spin
    # indefinitely against a live API.
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok([{"id": str(i)} for i in range(100)])

    with pytest.raises(RazorpayUnavailable, match=f"{MAX_PAGES} pages"):
        _client(handler).fetch_settlements(0, 1)


def test_a_non_list_items_field_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": {"not": "a list"}})

    with pytest.raises(RazorpayUnavailable, match="items"):
        _client(handler).fetch_settlements(0, 1)


# ---------------------------------------------------------------------------
# Rate limiting and errors
# ---------------------------------------------------------------------------


def test_a_429_is_retried_and_then_succeeds():
    responses = [httpx.Response(429), httpx.Response(429), _ok([{"id": "a"}])]
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return responses[attempts["n"] - 1]

    items = _client(handler).fetch_settlements(0, 1)

    assert items == [{"id": "a"}]
    assert attempts["n"] == 3


def test_a_sustained_429_degrades_after_exactly_max_retries_plus_one_attempts():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(429)

    with pytest.raises(RazorpayUnavailable, match="rate-limited"):
        _client(handler, max_retries=3).fetch_settlements(0, 1)

    assert attempts["n"] == 4, "one initial attempt plus max_retries, never an unbounded storm"


def test_a_non_429_error_fails_fast_without_retrying():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500)

    with pytest.raises(RazorpayUnavailable, match="HTTP 500"):
        _client(handler).fetch_settlements(0, 1)

    assert attempts["n"] == 1, "a 500 is not a rate limit; retrying it just multiplies the failure"


def test_a_non_json_response_is_a_named_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(RazorpayUnavailable, match="not valid JSON"):
        _client(handler).fetch_settlements(0, 1)


def test_the_client_counts_the_requests_it_made():
    def handler(request: httpx.Request) -> httpx.Response:
        return _ok([])

    client = _client(handler)
    client.fetch_settlements(0, 1)
    client.fetch_payments(0, 1)

    assert client.request_count == 2
