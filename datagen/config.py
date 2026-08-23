"""Fixed shape of one generated month (GenerationConfig) plus the
per-discrepancy-class instance counts (InjectionProfile) loaded from the
YAML files under datagen/profiles/ -- the only part of a run that's
YAML-configurable, per the working agreement against unrequested
configurability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from core.models import CardType, PaymentMethod


@dataclass(frozen=True)
class GenerationConfig:
    month: str = "2026-07"
    merchant_id: str = "MERCH-0001"
    mcc: str = "5411"
    total_payments: int = 5_200

    method_weights: dict[PaymentMethod, float] = field(
        default_factory=lambda: {
            PaymentMethod.UPI: 0.55,
            PaymentMethod.CARD: 0.30,
            PaymentMethod.NETBANKING: 0.10,
            PaymentMethod.WALLET: 0.05,
        }
    )
    card_type_weights: dict[CardType, float] = field(
        default_factory=lambda: {CardType.CREDIT: 0.60, CardType.DEBIT: 0.40}
    )
    international_rate_of_cards: float = 0.10

    amount_floor_paise: int = 4_900
    amount_cap_paise: int = 20_000_000
    lognormal_mu: float = 6.5
    lognormal_sigma: float = 1.15

    festival_week: int = 3
    festival_multiplier: float = 2.5
    weekend_multiplier: float = 0.7
    business_hours: tuple[int, int] = (9, 21)

    refund_rate: float = 0.04
    refund_partial_rate: float = 0.5
    refund_lag_days: tuple[int, int] = (1, 14)
    refund_partial_fraction_range: tuple[float, float] = (0.2, 0.8)

    chargeback_rate: float = 0.0015
    chargeback_reason_codes: tuple[str, ...] = ("10.4", "13.1", "4853", "4863", "4837")
    chargeback_raised_lag_days: tuple[int, int] = (5, 25)
    chargeback_won_rate: float = 0.5
    chargeback_lost_rate: float = 0.35
    chargeback_resolution_lag_days: tuple[int, int] = (3, 10)

    reserve_hold_bps: int = 300
    reserve_hold_release_days: int = 12
    goodwill_credit_rate: float = 0.002
    goodwill_credit_range_paise: tuple[int, int] = (5_000, 50_000)

    settlement_lag_days: int = 2


_PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
_BUILTIN_PROFILE_NAMES = ("clean", "realistic", "stress")


class InjectionProfile(BaseModel):
    """Absolute instance counts per discrepancy class, not rates -- loaded
    from a flat YAML mapping (datagen/profiles/*.yaml or a custom path)."""

    D01: int = Field(ge=0, default=0)
    D02: int = Field(ge=0, default=0)
    D03: int = Field(ge=0, default=0)
    D04: int = Field(ge=0, default=0)
    D05: int = Field(ge=0, default=0)
    D06: int = Field(ge=0, default=0)
    D07: int = Field(ge=0, default=0)
    D08: int = Field(ge=0, default=0)
    D09: int = Field(ge=0, default=0)
    D10: int = Field(ge=0, default=0)
    D11: int = Field(ge=0, default=0)
    D12: int = Field(ge=0, default=0)

    @classmethod
    def from_yaml(cls, path: Path) -> InjectionProfile:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(**data)


def load_profile(name_or_path: str) -> InjectionProfile:
    """Resolves one of the three built-in profile names to
    datagen/profiles/<name>.yaml; anything else is treated as a path to a
    custom profile."""
    if name_or_path in _BUILTIN_PROFILE_NAMES:
        return InjectionProfile.from_yaml(_PROFILES_DIR / f"{name_or_path}.yaml")
    return InjectionProfile.from_yaml(Path(name_or_path))
