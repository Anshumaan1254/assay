"""Money value object. Integer paise, never a float.

Division is forbidden because it's lossy — use split_proportionally, which
guarantees its parts re-sum exactly to the original via the largest-remainder
method.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import total_ordering


@total_ordering
@dataclass(frozen=True, slots=True)
class Money:
    paise: int
    currency: str = "INR"

    def __post_init__(self) -> None:
        if isinstance(self.paise, bool) or not isinstance(self.paise, int):
            raise TypeError(f"Money.paise must be int, got {type(self.paise).__name__}")
        if not isinstance(self.currency, str) or not self.currency:
            raise ValueError("Money.currency must be a non-empty string")

    @classmethod
    def zero(cls, currency: str = "INR") -> Money:
        return cls(0, currency)

    @classmethod
    def from_rupees(cls, value: str | Decimal, currency: str = "INR") -> Money:
        if isinstance(value, float):
            raise TypeError("Money.from_rupees does not accept float; pass a str or Decimal")
        if isinstance(value, str):
            value = Decimal(value)
        if not isinstance(value, Decimal):
            raise TypeError(f"Money.from_rupees expects str or Decimal, got {type(value).__name__}")
        paise = int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        return cls(paise, currency)

    def to_rupees_str(self) -> str:
        sign = "-" if self.paise < 0 else ""
        whole, frac = divmod(abs(self.paise), 100)
        return f"{sign}{whole}.{frac:02d}"

    def _require_same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.paise + other.paise, self.currency)

    def __radd__(self, other: object) -> Money:
        if other == 0:
            return self
        if isinstance(other, Money):
            return other.__add__(self)
        return NotImplemented

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.paise - other.paise, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.paise, self.currency)

    def __mul__(self, n: object) -> Money:
        if isinstance(n, bool) or not isinstance(n, int):
            raise TypeError(f"Money can only be multiplied by int, got {type(n).__name__}")
        return Money(self.paise * n, self.currency)

    __rmul__ = __mul__

    def __truediv__(self, other: object) -> Money:
        raise TypeError("Money division is lossy and forbidden; use split_proportionally")

    def __floordiv__(self, other: object) -> Money:
        raise TypeError("Money division is lossy and forbidden; use split_proportionally")

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return self.paise < other.paise

    def split_proportionally(self, weights: Sequence[int]) -> list[Money]:
        if not weights:
            raise ValueError("cannot split into zero parts")
        for w in weights:
            if isinstance(w, bool) or not isinstance(w, int):
                raise TypeError(f"weights must be int, got {type(w).__name__}")
            if w < 0:
                raise ValueError("weights must be non-negative")
        total_weight = sum(weights)
        if total_weight == 0:
            raise ValueError("cannot split by all-zero weights")

        shares: list[int] = []
        remainders: list[int] = []
        for w in weights:
            share, remainder = divmod(self.paise * w, total_weight)
            shares.append(share)
            remainders.append(remainder)

        leftover = self.paise - sum(shares)
        order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
        for i in order[:leftover]:
            shares[i] += 1

        return [Money(share, self.currency) for share in shares]
