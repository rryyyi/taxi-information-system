"""计价规则求值（唯一实现，供预估金额校验与归因共用）。

公式（元）：
    metered = base_fare + per_km * km + per_min * minutes
    metered = max(metered, min_fare)          # 最低消费
    fare    = metered * surge_multiplier + Σsurcharges - Σdiscounts

所有中间值用 Decimal 计算，最终 ROUND_HALF_UP 量化到 0.01 元。
动态加价系数（surge）只作用于里程+时长+起步价部分，不作用于路侧杂费。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def money(value: Decimal | float | int | str) -> Decimal:
    """金额量化：统一保留两位小数（ROUND_HALF_UP）。"""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def to_decimal(value: Decimal | float | int | str | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


@dataclass(frozen=True)
class PricingParams:
    base_fare: Decimal
    per_km: Decimal
    per_min: Decimal
    min_fare: Decimal
    surge_multiplier: Decimal

    @classmethod
    def from_rule(cls, rule) -> "PricingParams":
        return cls(
            base_fare=to_decimal(rule.base_fare),
            per_km=to_decimal(rule.per_km),
            per_min=to_decimal(rule.per_min),
            min_fare=to_decimal(rule.min_fare),
            surge_multiplier=to_decimal(rule.surge_multiplier),
        )

    def as_evidence(self) -> dict:
        return {
            "base_fare": str(money(self.base_fare)),
            "per_km": str(money(self.per_km)),
            "per_min": str(money(self.per_min)),
            "min_fare": str(money(self.min_fare)),
            "surge_multiplier": str(self.surge_multiplier.quantize(Decimal("0.01"))),
        }


def sum_fees(items: dict | None) -> Decimal:
    """杂费/优惠字典求和（值可为数字或字符串）。"""
    if not items:
        return Decimal("0")
    return sum((to_decimal(v) for v in items.values()), Decimal("0"))


def evaluate(
    params: PricingParams,
    km: Decimal | float,
    minutes: Decimal | float,
    *,
    surge_multiplier: Decimal | float | None = None,
    surcharges: Decimal | float = 0,
    discounts: Decimal | float = 0,
    apply_min_fare: bool = True,
) -> Decimal:
    kmd = to_decimal(km)
    mind = to_decimal(minutes)
    surge = to_decimal(surge_multiplier) if surge_multiplier is not None else params.surge_multiplier

    metered = params.base_fare + params.per_km * kmd + params.per_min * mind
    if apply_min_fare and params.min_fare > 0:
        metered = max(metered, params.min_fare)
    fare = metered * surge + to_decimal(surcharges) - to_decimal(discounts)
    return money(fare)
