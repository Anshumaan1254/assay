"""Distribution-shape sanity for sampling.py -- statistical, not exact, but
still deterministic pass/fail given a fixed seed."""

from __future__ import annotations

from collections import Counter
from random import Random

import pytest

from core.models import CardType, Network, PaymentMethod
from datagen.sampling import (
    sample_amount_paise,
    sample_card_type,
    sample_method,
    sample_network,
    weighted_choice,
)


def test_sample_amount_paise_respects_floor():
    rng = Random(1)
    for _ in range(2_000):
        paise = sample_amount_paise(rng, mu=1.0, sigma=0.5, floor_paise=4_900, cap_paise=20_000_000)
        assert paise >= 4_900


def test_sample_amount_paise_respects_cap():
    rng = Random(1)
    for _ in range(2_000):
        paise = sample_amount_paise(rng, mu=12.0, sigma=3.0, floor_paise=4_900, cap_paise=20_000_000)
        assert paise <= 20_000_000


def test_sample_amount_paise_deterministic_with_seed():
    a = sample_amount_paise(Random(99), mu=6.5, sigma=1.15, floor_paise=4_900, cap_paise=20_000_000)
    b = sample_amount_paise(Random(99), mu=6.5, sigma=1.15, floor_paise=4_900, cap_paise=20_000_000)
    assert a == b


def test_weighted_choice_never_returns_a_zero_weight_key():
    rng = Random(3)
    weights = {"a": 1.0, "b": 0.0}
    results = {weighted_choice(rng, weights) for _ in range(200)}
    assert results == {"a"}


def test_sample_method_matches_configured_weights_within_tolerance():
    rng = Random(2)
    weights = {
        PaymentMethod.UPI: 0.55,
        PaymentMethod.CARD: 0.30,
        PaymentMethod.NETBANKING: 0.10,
        PaymentMethod.WALLET: 0.05,
    }
    n = 20_000
    counts = Counter(sample_method(rng, weights) for _ in range(n))
    for method, weight in weights.items():
        assert counts[method] / n == pytest.approx(weight, abs=0.02)


def test_sample_card_type_matches_configured_weights_within_tolerance():
    rng = Random(4)
    weights = {CardType.CREDIT: 0.60, CardType.DEBIT: 0.40}
    n = 20_000
    counts = Counter(sample_card_type(rng, weights) for _ in range(n))
    for card_type, weight in weights.items():
        assert counts[card_type] / n == pytest.approx(weight, abs=0.02)


def test_sample_network_domestic_includes_rupay():
    rng = Random(5)
    networks = {sample_network(rng, is_international=False) for _ in range(500)}
    assert Network.RUPAY in networks


def test_sample_network_international_excludes_rupay():
    rng = Random(6)
    networks = {sample_network(rng, is_international=True) for _ in range(500)}
    assert Network.RUPAY not in networks
