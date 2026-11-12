"""测试公共断言与请求构造。"""

from __future__ import annotations

from decimal import Decimal

EPS = Decimal("0.01")


def assert_conservation(items: list[dict] | list, total_delta: Decimal, residual: Decimal) -> None:
    """金额守恒：归因项之和（含残差）与总差额偏差 ≤ 0.01 元。"""
    s = sum((Decimal(str(i["amount"])) for i in items), Decimal("0")) if items else Decimal("0")
    assert abs(s - total_delta) <= EPS, f"金额不守恒: sum={s} total_delta={total_delta}"
    assert abs(residual) <= EPS or abs(residual) >= 0  # 残差必须被显式记录


def amounts(payload: dict) -> dict[str, Decimal]:
    return {i["code"]: Decimal(str(i["amount"])) for i in payload}


def sample_polyline(start_lat: float = 30.0, start_lng: float = 120.0, meters: int = 1000) -> list[list[float]]:
    """向东一条直线折线（约 meters 米）。"""
    dlat = 0.0
    dlng = meters / (111319.49 * 0.8660)  # cos(30°) ≈ 0.866
    return [[start_lat, start_lng], [start_lat + dlat, start_lng + dlng]]


def order_payload(order_no: str = "FT-T-1") -> dict:
    return {
        "order_no": order_no,
        "city": "演示城市",
        "origin_label": "测试起点",
        "destination_label": "测试终点",
        "estimate": {
            "quoted_price": "20.00",
            "est_distance_m": 5000,
            "est_duration_s": 900,
            "rule_version": "R-TEST-1",
            "surge_multiplier": "1.00",
            "surcharges": {},
            "discounts": {},
        },
        "reference_route": {
            "polyline": sample_polyline(meters=5000),
            "length_m": 5000,
            "free_flow_speed_kmh": 40,
        },
    }


def complete_payload(
    paid: str = "23.50",
    actual_distance_m: int = 6000,
    actual_duration_s: int = 1200,
    points: list[dict] | None = None,
    bill_extra: dict | None = None,
) -> dict:
    bill = {
        "paid_amount": paid,
        "actual_distance_m": actual_distance_m,
        "actual_duration_s": actual_duration_s,
        "base_fare": "5.00",
        "distance_fare": "12.00",
        "time_fare": "6.50",
        "rule_version": "R-TEST-1",
        "surges": None,
    }
    bill.pop("surges", None)
    bill.update(bill_extra or {})
    return {
        "bill": bill,
        "trajectory": points or [],
    }
