"""Tests for core/money.py.

Money is integer paise. No float ever touches it. split_proportionally is the
only way to divide a Money value, and it must always re-sum exactly to the
original — that's the property test hypothesis drives at the end of this file.
"""

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from core.money import Money

# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_construct_with_int_paise_and_default_currency():
    m = Money(12345)
    assert m.paise == 12345
    assert m.currency == "INR"


def test_construct_with_explicit_currency():
    m = Money(100, "USD")
    assert m.currency == "USD"


def test_construct_rejects_float_paise():
    with pytest.raises(TypeError):
        Money(12.5)


def test_construct_rejects_bool_paise():
    with pytest.raises(TypeError):
        Money(True)


def test_construct_rejects_str_paise():
    with pytest.raises(TypeError):
        Money("100")


def test_construct_rejects_empty_currency():
    with pytest.raises(ValueError):
        Money(100, "")


def test_zero_classmethod():
    assert Money.zero() == Money(0, "INR")
    assert Money.zero("USD") == Money(0, "USD")


# ---------------------------------------------------------------------------
# repr — must show paise, not rupees
# ---------------------------------------------------------------------------


def test_repr_shows_paise_not_rupees():
    assert repr(Money(12345, "INR")) == "Money(paise=12345, currency='INR')"


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------


def test_add_same_currency():
    assert Money(100) + Money(50) == Money(150)


def test_add_different_currency_raises():
    with pytest.raises(ValueError):
        Money(100, "INR") + Money(50, "USD")


def test_add_non_money_raises():
    with pytest.raises(TypeError):
        Money(100) + 50


def test_sub_same_currency():
    assert Money(100) - Money(30) == Money(70)


def test_sub_can_go_negative():
    assert Money(30) - Money(100) == Money(-70)


def test_sub_different_currency_raises():
    with pytest.raises(ValueError):
        Money(100, "INR") - Money(50, "USD")


def test_neg():
    assert -Money(100) == Money(-100)
    assert -Money(-100) == Money(100)


def test_mul_by_int():
    assert Money(100) * 3 == Money(300)
    assert 3 * Money(100) == Money(300)


def test_mul_by_float_raises():
    with pytest.raises(TypeError):
        Money(100) * 1.5


def test_mul_by_zero_and_negative():
    assert Money(100) * 0 == Money(0)
    assert Money(100) * -2 == Money(-200)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def test_equality_same_currency():
    assert Money(100, "INR") == Money(100, "INR")
    assert Money(100, "INR") != Money(200, "INR")


def test_equality_different_currency_is_false_not_raise():
    assert (Money(100, "INR") == Money(100, "USD")) is False


def test_ordering_same_currency():
    assert Money(50) < Money(100)
    assert Money(100) <= Money(100)
    assert Money(150) > Money(100)
    assert Money(100) >= Money(100)


def test_ordering_different_currency_raises():
    with pytest.raises(ValueError):
        _ = Money(50, "INR") < Money(100, "USD")


def test_hashable_and_usable_in_sets():
    assert len({Money(100), Money(100), Money(200)}) == 2


# ---------------------------------------------------------------------------
# Division is always forbidden
# ---------------------------------------------------------------------------


def test_true_division_raises():
    with pytest.raises(TypeError):
        Money(100) / 2


def test_floor_division_raises():
    with pytest.raises(TypeError):
        Money(100) // 2


# ---------------------------------------------------------------------------
# sum()
# ---------------------------------------------------------------------------


def test_sum_with_default_start():
    assert sum([Money(100), Money(200), Money(300)]) == Money(600)


def test_sum_empty_list_with_default_start_is_int_zero():
    assert sum([]) == 0


def test_sum_with_explicit_money_start():
    assert sum([Money(100), Money(200)], Money.zero()) == Money(300)


# ---------------------------------------------------------------------------
# from_rupees / to_rupees_str
# ---------------------------------------------------------------------------


def test_from_rupees_str():
    assert Money.from_rupees("123.45") == Money(12345)


def test_from_rupees_decimal():
    assert Money.from_rupees(Decimal("123.45")) == Money(12345)


def test_from_rupees_rejects_float():
    with pytest.raises(TypeError):
        Money.from_rupees(123.45)


def test_from_rupees_whole_rupees():
    assert Money.from_rupees("100") == Money(10000)


def test_from_rupees_negative():
    assert Money.from_rupees("-50.25") == Money(-5025)


def test_from_rupees_rounds_half_up():
    # 1.005 rupees == 100.5 paise -> rounds up to 101
    assert Money.from_rupees("1.005") == Money(101)


def test_to_rupees_str_positive():
    assert Money(12345).to_rupees_str() == "123.45"


def test_to_rupees_str_negative():
    assert Money(-12345).to_rupees_str() == "-123.45"


def test_to_rupees_str_small_amount():
    assert Money(5).to_rupees_str() == "0.05"


def test_to_rupees_str_zero():
    assert Money(0).to_rupees_str() == "0.00"


def test_from_rupees_to_rupees_str_round_trip():
    for rupees in ["0.00", "1.00", "123.45", "-99.99", "10000.01"]:
        assert Money.from_rupees(rupees).to_rupees_str() == rupees


# ---------------------------------------------------------------------------
# split_proportionally — example cases
# ---------------------------------------------------------------------------


def test_split_even():
    parts = Money(300).split_proportionally([1, 1, 1])
    assert parts == [Money(100), Money(100), Money(100)]


def test_split_uneven_resums_and_distributes_remainder():
    parts = Money(100).split_proportionally([1, 1, 1])
    assert sum(parts, Money.zero()) == Money(100)
    # largest-remainder method: two get 34, one gets 32, or similar split
    # within a paisa of each other
    assert max(p.paise for p in parts) - min(p.paise for p in parts) <= 1


def test_split_single_weight_returns_whole():
    assert Money(100).split_proportionally([1]) == [Money(100)]


def test_split_weighted_by_proportion():
    parts = Money(100).split_proportionally([1, 3])
    assert sum(parts, Money.zero()) == Money(100)
    assert parts[1].paise > parts[0].paise


def test_split_zero_weight_entry_gets_nothing():
    parts = Money(100).split_proportionally([0, 1])
    assert parts[0] == Money(0)
    assert parts[1] == Money(100)


def test_split_negative_money_resums_correctly():
    parts = Money(-100).split_proportionally([1, 1, 1])
    assert sum(parts, Money.zero()) == Money(-100)


def test_split_empty_weights_raises():
    with pytest.raises(ValueError):
        Money(100).split_proportionally([])


def test_split_all_zero_weights_raises():
    with pytest.raises(ValueError):
        Money(100).split_proportionally([0, 0])


def test_split_negative_weight_raises():
    with pytest.raises(ValueError):
        Money(100).split_proportionally([1, -1])


def test_split_float_weight_raises():
    with pytest.raises(TypeError):
        Money(100).split_proportionally([1.0, 1])


# ---------------------------------------------------------------------------
# Property test: split_proportionally always re-sums to the original,
# for random paise (including negative) and random weight vectors.
# ---------------------------------------------------------------------------


@given(
    paise=st.integers(min_value=-10**12, max_value=10**12),
    weights=st.lists(
        st.integers(min_value=0, max_value=10**6), min_size=1, max_size=25
    ).filter(lambda ws: sum(ws) > 0),
)
def test_split_proportionally_always_resums_to_original(paise, weights):
    m = Money(paise, "INR")
    parts = m.split_proportionally(weights)
    assert len(parts) == len(weights)
    assert sum(parts, Money.zero("INR")) == m
