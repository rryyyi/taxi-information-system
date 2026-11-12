"""计价规则求值测试（元 / 2 位小数）。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.pricing import PricingParams, evaluate, money, sum_fees, to_decimal

PARAMS = PricingParams(
    base_fare=Decimal("5.00"),
    per_km=Decimal("2.00"),
    per_min=Decimal("0.40"),
    min_fare=Decimal("0.00"),
    surge_multiplier=Decimal("1.00"),
)


def test_money_quantizes_half_up() -> None:
    assert money("1.005") == Decimal("1.01")
    assert money(2.344) == Decimal("2.34")
    assert str(money(Decimal("18"))) == "18.00"


def test_evaluate_basic_formula() -> None:
    # 5 + 2*5 + 0.4*15 = 21.00
    assert evaluate(PARAMS, 5.0, 15.0) == Decimal("21.00")


def test_evaluate_min_fare_clamp() -> None:
    params = PricingParams(
        base_fare=Decimal("3.00"),
        per_km=Decimal("1.00"),
        per_min=Decimal("0.10"),
        min_fare=Decimal("12.00"),
        surge_multiplier=Decimal("1.00"),
    )
    assert evaluate(params, 1.0, 1.0) == Decimal("12.00")
    assert evaluate(params, 3.0, 1.0) == Decimal("12.00")
    assert evaluate(params, 10.0, 10.0) == Decimal("14.00")


def test_evaluate_surge_applies_to_metered_only() -> None:
    params = PricingParams(
        base_fare=Decimal("5.00"),
        per_km=Decimal("2.00"),
        per_min=Decimal("0.40"),
        min_fare=Decimal("0.00"),
        surge_multiplier=Decimal("1.00"),
    )
    # metered = 5 + 2*5 + 0.4*10 = 19.00 -> *1.5 = 28.50；杂费不参与加价
    assert evaluate(params, 5.0, 10.0, surge_multiplier=1.5) == Decimal("28.50")
    assert evaluate(params, 5.0, 10.0, surge_multiplier=1.5, surcharges=Decimal("3.00")) == Decimal("31.50")


def test_evaluate_surcharge_and_discount() -> None:
    fare = evaluate(PARAMS, 5.0, 15.0, surcharges=Decimal("0.55"), discounts=Decimal("0.40"))
    assert fare == Decimal("21.15")


def test_sum_fees_handles_strings_and_none() -> None:
    assert sum_fees({"过路费": "0.55", "停车费": 1}) == Decimal("1.55")
    assert sum_fees(None) == Decimal("0")
    assert to_decimal(None) == Decimal("0")


def test_evaluate_is_deterministic() -> None:
    first = evaluate(PARAMS, 6.8, 34.0)
    second = evaluate(PARAMS, 6.8, 34.0)
    assert first == second


@pytest.mark.parametrize(
    "km, minutes, expected",
    [
        (4.8, 20.0, "18.00"),   # FT-1800 预估价
        (6.8, 34.0, "27.00"),   # FT-1800 实付（换成 R-1800-BILL 费率时见下）
    ],
)
def test_ft1800_reference_numbers(km: float, minutes: float, expected: str) -> None:
    params = PricingParams(
        base_fare=Decimal("4.56"),
        per_km=Decimal("1.55"),
        per_min=Decimal("0.30"),
        min_fare=Decimal("0.00"),
        surge_multiplier=Decimal("1.00"),
    )
    if expected == "27.00":
        params = PricingParams(
            base_fare=Decimal("6.26"),
            per_km=Decimal("1.55"),
            per_min=Decimal("0.30"),
            min_fare=Decimal("0.00"),
            surge_multiplier=Decimal("1.00"),
        )
    assert evaluate(params, km, minutes) == Decimal(expected)
