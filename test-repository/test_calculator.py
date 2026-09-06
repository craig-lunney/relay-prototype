"""Tests for the calculator module."""
import pytest
from calculator import (
    add,
    subtract,
    multiply,
    divide,
    calculate_discount_price,
    calculate_bundle_price,
)


# ── Basic arithmetic ──────────────────────────────────────────────────────────

def test_add():
    assert add(2, 3) == 5

def test_subtract():
    assert subtract(5, 3) == 2

def test_multiply():
    assert multiply(4, 3) == 12

def test_divide():
    assert divide(10, 2) == 5.0

def test_divide_by_zero():
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)


# ── Discount calculation ──────────────────────────────────────────────────────

def test_discount_round_number():
    # 10% off £100.00 → £90.00 (passes even with the floor-division bug)
    assert calculate_discount_price(100.0, 10.0) == pytest.approx(90.0, abs=0.01)

def test_discount_non_round_price():
    # 15% off £99.99 → £84.99
    # FAILS with the bug: floor-division gives 14.0 instead of 14.9985 discount
    # so result is £85.99 instead of £84.99.
    assert calculate_discount_price(99.99, 15.0) == pytest.approx(84.99, abs=0.01)

def test_discount_non_round_percent():
    # 10% off £33.33 → £30.00
    # FAILS with the bug: 33.33 * 10 // 100 = 3.0 instead of 3.333, result is £30.33.
    assert calculate_discount_price(33.33, 10.0) == pytest.approx(30.00, abs=0.01)

def test_discount_small_price():
    # 20% off £4.99 → £3.99
    # FAILS with bug: 4.99 * 20 // 100 = 0.0, result is £4.99 (no discount applied).
    assert calculate_discount_price(4.99, 20.0) == pytest.approx(3.99, abs=0.01)

def test_discount_zero_percent():
    assert calculate_discount_price(100.0, 0.0) == pytest.approx(100.0, abs=0.01)

def test_discount_full_100_percent():
    assert calculate_discount_price(100.0, 100.0) == pytest.approx(0.0, abs=0.01)

def test_discount_invalid_negative():
    with pytest.raises(ValueError):
        calculate_discount_price(100.0, -1.0)

def test_discount_invalid_over_100():
    with pytest.raises(ValueError):
        calculate_discount_price(100.0, 101.0)


# ── Bundle pricing ────────────────────────────────────────────────────────────

def test_bundle_basic():
    # 10% off [£10.00, £20.00, £30.00] = £60.00 * 0.90 = £54.00
    assert calculate_bundle_price([10.0, 20.0, 30.0], 10.0) == pytest.approx(54.0, abs=0.01)

def test_bundle_non_round_items():
    # 15% off [£9.99, £14.99] = £24.98 * 0.85 = £21.233 → £21.23
    assert calculate_bundle_price([9.99, 14.99], 15.0) == pytest.approx(21.23, abs=0.01)

def test_bundle_single_item():
    assert calculate_bundle_price([50.0], 20.0) == pytest.approx(40.0, abs=0.01)

def test_bundle_empty_raises():
    with pytest.raises(ValueError):
        calculate_bundle_price([], 10.0)
