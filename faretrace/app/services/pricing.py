"""金额工具：统一 Decimal + ROUND_HALF_UP，保留两位小数（单位：元）。"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")


def money(value: Decimal | float | int | str | None) -> Decimal:
    """金额量化：统一保留两位小数（ROUND_HALF_UP）。"""
    return Decimal(str(value if value is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def to_decimal(value: Decimal | float | int | str | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))
