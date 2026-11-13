"""测试公共断言与请求构造（沈阳版）。"""

from __future__ import annotations

from decimal import Decimal

EPS = Decimal("0.01")


def assert_conservation(items: list[dict], total_delta: Decimal) -> None:
    """金额守恒：归因项之和（含残差）与总差额偏差 ≤ 0.01 元。"""
    s = sum((Decimal(str(i["amount"])) for i in items), Decimal("0"))
    assert abs(s - total_delta) <= EPS, f"金额不守恒: sum={s} total_delta={total_delta}"


def amounts(payload: list[dict]) -> dict[str, Decimal]:
    return {i["code"]: Decimal(str(i["amount"])) for i in payload}


def sample_polyline(start_lat: float = 41.8015, start_lng: float = 123.4352, meters: int = 3000) -> list[list[float]]:
    """沿青年大街（lng≈123.4359）向南的直线折线，用于接口测试。"""
    dlat = -meters / 111319.49
    return [[round(start_lat, 6), start_lng], [round(start_lat + dlat, 6), start_lng]]


def order_payload(order_no: str = "SY-T-1") -> dict:
    return {
        "order_no": order_no,
        "origin_label": "市府广场",
        "destination_label": "浑河桥南",
        "requested_at": "2026-03-06T10:00:00",
        "estimate": {
            "quoted_price": "15.00",
            "est_distance_m": 3000,
            "est_duration_s": 300,
        },
        "reference_route": {
            "polyline": sample_polyline(meters=3000),
            "length_m": 3000,
            "node_names": ["市府广场", "浑河桥南"],
        },
    }


def complete_payload(
    paid: str = "15.00",
    actual_distance_m: int = 3000,
    actual_duration_s: int = 300,
    points: list[dict] | None = None,
    completed_at: str = "2026-03-06T10:05:00",
) -> dict:
    return {
        "bill": {
            "paid_amount": paid,
            "actual_distance_m": actual_distance_m,
            "actual_duration_s": actual_duration_s,
        },
        "trajectory": points or [],
        "completed_at": completed_at,
    }
