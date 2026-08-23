"""Calendar helpers for the generated month: a weekly volume pattern with
one festival spike week, T+2 settlement-cycle boundaries, and an
exact-sum count distributor.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from random import Random

from core.models import IST


def month_bounds(month: str) -> tuple[date, date]:
    """(first_day, last_day) of a 'YYYY-MM' month, inclusive."""
    year, mon = (int(p) for p in month.split("-"))
    last_day = calendar.monthrange(year, mon)[1]
    return date(year, mon, 1), date(year, mon, last_day)


def day_weight(day: date, festival_week: int, festival_multiplier: float, weekend_multiplier: float) -> float:
    """Relative payment-volume weight for one calendar day. `festival_week`
    is 1-indexed week-of-month (1 = days 1-7, 2 = days 8-14, ...); Saturday
    and Sunday additionally get `weekend_multiplier`."""
    week_of_month = (day.day - 1) // 7 + 1
    weight = festival_multiplier if week_of_month == festival_week else 1.0
    if day.weekday() >= 5:  # Saturday=5, Sunday=6
        weight *= weekend_multiplier
    return weight


def distribute_counts(total: int, weights: list[float]) -> list[int]:
    """Splits `total` whole units across `weights` proportionally, with an
    exact sum, via the largest-remainder method. Deliberately not
    core.money.Money.split_proportionally: these are payment counts, not
    paise, and reusing a Money-typed method for a non-money quantity would
    be a type-abuse smell."""
    if not weights:
        raise ValueError("cannot distribute across zero buckets")
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("weights must sum to a positive number")

    raw = [total * w / total_weight for w in weights]
    shares = [int(r) for r in raw]
    remainders = [r - s for r, s in zip(raw, shares)]

    leftover = total - sum(shares)
    order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for i in order[:leftover]:
        shares[i] += 1
    return shares


def settlement_batch_id(cycle_day: date) -> str:
    """The T+2 SettlementBatch id the given calendar day (IST) nets into."""
    return f"STL-{cycle_day:%Y%m%d}"


def credit_value_date(cycle_day: date, settlement_lag_days: int = 2) -> date:
    """BankCredit.value_date for a batch whose cycle is `cycle_day` — a
    simple +N calendar days, not business-day-adjusted (a stated
    simplification, not an oversight)."""
    return cycle_day + timedelta(days=settlement_lag_days)


def ist_moment(day: date, rng: Random, business_hours: tuple[int, int] = (9, 21)) -> datetime:
    """A random tz-aware IST timestamp on `day`, uniform within
    `business_hours` (24h, [start, end))."""
    start_h, end_h = business_hours
    total_seconds = (end_h - start_h) * 3600
    offset = rng.randint(0, total_seconds - 1)
    return datetime(day.year, day.month, day.day, start_h, 0, tzinfo=IST) + timedelta(seconds=offset)
