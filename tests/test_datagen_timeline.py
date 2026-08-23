"""Calendar/weekly-pattern helpers -- pure date math and an exact-sum count
distributor, no money involved."""

from __future__ import annotations

from datetime import date

import pytest

from datagen.timeline import (
    credit_value_date,
    day_weight,
    distribute_counts,
    ist_moment,
    month_bounds,
    settlement_batch_id,
)


def test_month_bounds_july_2026():
    first, last = month_bounds("2026-07")
    assert first == date(2026, 7, 1)
    assert last == date(2026, 7, 31)


def test_month_bounds_handles_february_leap_year():
    first, last = month_bounds("2024-02")
    assert last == date(2024, 2, 29)


def test_day_weight_festival_week_gets_multiplier():
    # 2026-07-15 is a Wednesday, week 3 of the month (days 15-21).
    weight = day_weight(date(2026, 7, 15), festival_week=3, festival_multiplier=2.5, weekend_multiplier=0.7)
    assert weight == 2.5


def test_day_weight_normal_weekday_is_baseline_one():
    weight = day_weight(date(2026, 7, 1), festival_week=3, festival_multiplier=2.5, weekend_multiplier=0.7)
    assert weight == 1.0


def test_day_weight_weekend_multiplier_applies_outside_festival_week():
    # 2026-07-04 is a Saturday, week 1.
    weight = day_weight(date(2026, 7, 4), festival_week=3, festival_multiplier=2.5, weekend_multiplier=0.7)
    assert weight == pytest.approx(0.7)


def test_day_weight_festival_and_weekend_multipliers_compose():
    # 2026-07-18 is a Saturday in festival week 3.
    weight = day_weight(date(2026, 7, 18), festival_week=3, festival_multiplier=2.5, weekend_multiplier=0.7)
    assert weight == pytest.approx(2.5 * 0.7)


def test_distribute_counts_sums_exactly():
    for total in (0, 1, 5, 31, 5200, 5201, 9999):
        weights = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        shares = distribute_counts(total, weights)
        assert sum(shares) == total
        assert all(s >= 0 for s in shares)


def test_distribute_counts_favors_higher_weight():
    shares = distribute_counts(100, [3.0, 1.0])
    assert shares[0] > shares[1]


def test_distribute_counts_is_deterministic():
    weights = [2.5, 1.0, 0.7, 1.0, 1.0, 1.0, 0.7]
    assert distribute_counts(5200, weights) == distribute_counts(5200, weights)


def test_distribute_counts_rejects_empty_weights():
    with pytest.raises(ValueError):
        distribute_counts(10, [])


def test_distribute_counts_rejects_all_zero_weights():
    with pytest.raises(ValueError):
        distribute_counts(10, [0.0, 0.0])


def test_settlement_batch_id_format():
    assert settlement_batch_id(date(2026, 7, 5)) == "STL-20260705"


def test_credit_value_date_adds_lag():
    assert credit_value_date(date(2026, 7, 5), settlement_lag_days=2) == date(2026, 7, 7)


def test_ist_moment_is_timezone_aware_and_within_business_hours():
    from random import Random

    rng = Random(42)
    moment = ist_moment(date(2026, 7, 10), rng, business_hours=(9, 21))
    assert moment.tzinfo is not None
    assert moment.date() == date(2026, 7, 10)
    assert 9 <= moment.hour < 21


def test_ist_moment_deterministic_with_seed():
    from random import Random

    m1 = ist_moment(date(2026, 7, 10), Random(7))
    m2 = ist_moment(date(2026, 7, 10), Random(7))
    assert m1 == m2
