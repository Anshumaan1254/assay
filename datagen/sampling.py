"""Amount, method, card-type, and network sampling. Every draw takes a
caller-supplied seeded `random.Random` explicitly — never the global
`random` module — so a whole run is reproducible end to end from one seed.
"""

from __future__ import annotations

from random import Random
from typing import TypeVar

from core.models import CardType, Network, PaymentMethod

T = TypeVar("T")


def weighted_choice(rng: Random, weights: dict[T, float]) -> T:
    keys = list(weights.keys())
    probs = list(weights.values())
    return rng.choices(keys, weights=probs, k=1)[0]


def sample_amount_paise(rng: Random, mu: float, sigma: float, floor_paise: int, cap_paise: int) -> int:
    """Log-normal draw in rupees, converted to paise and clamped to
    [floor_paise, cap_paise]. The float math here is RNG sampling, not a
    Money computation — it never touches a Money instance or a Money field
    directly; the result is a plain int paise value from the moment it's
    created."""
    rupees = rng.lognormvariate(mu, sigma)
    paise = round(rupees * 100)
    return max(floor_paise, min(cap_paise, paise))


def sample_method(rng: Random, method_weights: dict[PaymentMethod, float]) -> PaymentMethod:
    return weighted_choice(rng, method_weights)


def sample_card_type(rng: Random, card_type_weights: dict[CardType, float]) -> CardType:
    return weighted_choice(rng, card_type_weights)


# RuPay is a domestic-only scheme in practice, so it's excluded (and the
# rest renormalized) from the international pool rather than just given a
# zero weight in the same table -- keeps the two tables independently
# readable.
_DOMESTIC_NETWORK_WEIGHTS: dict[Network, float] = {
    Network.VISA: 0.45,
    Network.MASTERCARD: 0.30,
    Network.RUPAY: 0.20,
    Network.AMEX: 0.05,
}
_INTERNATIONAL_NETWORK_WEIGHTS: dict[Network, float] = {
    Network.VISA: 0.55,
    Network.MASTERCARD: 0.35,
    Network.AMEX: 0.10,
}


def sample_network(rng: Random, is_international: bool) -> Network:
    weights = _INTERNATIONAL_NETWORK_WEIGHTS if is_international else _DOMESTIC_NETWORK_WEIGHTS
    return weighted_choice(rng, weights)
