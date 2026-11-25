"""归因核心测试：金额守恒、等候/绕路事件、返空费、夜间档、残差待核查、确定性。"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.models import Order
from app.services.attribution import run_attribution

EPS = Decimal("0.01")
CODES = {
    "WAIT_FEE",
    "DETOUR_EXTRA",
    "MILEAGE_EXTRA",
    "RETURN_FEE",
    "NIGHT_SURCHARGE",
    "PRICING_RULE_CHANGE",
    "UNEXPLAINED_RESIDUAL",
}


def _amounts(attribution) -> dict[str, Decimal]:
    return {item.code: Decimal(item.amount) for item in attribution.items}


def _conserved(attribution) -> bool:
    total = Decimal(attribution.total_delta)
    amount_sum = sum((Decimal(i.amount) for i in attribution.items), Decimal("0"))
    return abs(amount_sum - total) <= EPS


@pytest.fixture()
def seeded(seeded_session: Session) -> Session:
    return seeded_session


def _order(session: Session, order_no: str) -> Order:
    return session.scalars(select(Order).where(Order.order_no == order_no)).one()


# --------------------------------------------------------------------------- #
# 守恒与归因项全集
# --------------------------------------------------------------------------- #
def test_all_seeds_conserve_money(seeded: Session) -> None:
    orders = seeded.scalars(select(Order)).all()
    assert len(orders) == 7
    for order in orders:
        attribution = order.attribution
        assert attribution is not None
        assert _conserved(attribution), f"{order.order_no} 不守恒"
        assert set(_amounts(attribution)) == CODES


def test_pricing_rule_change_is_always_zero(seeded: Session) -> None:
    """沈阳计价规则为固定基准：日常归因中"计价规则变化"恒为 0 元。"""
    for order in seeded.scalars(select(Order)).all():
        assert _amounts(order.attribution)["PRICING_RULE_CHANGE"] == Decimal("0.00")
        item = {i.code: i for i in order.attribution.items}["PRICING_RULE_CHANGE"]
        assert "固定基准" in item.evidence_json["formula"]


def test_evidence_bundle_conservation_flag(seeded: Session) -> None:
    for order in seeded.scalars(select(Order)).all():
        bundle = order.attribution.evidence_bundle_json
        assert bundle["totals"]["conservation_ok"] is True


# --------------------------------------------------------------------------- #
# 生活化验收：FT-SY-1800（预估 18.00 → 实付 27.00）
# --------------------------------------------------------------------------- #
def test_ft_sy_1800_acceptance(seeded: Session) -> None:
    order = _order(seeded, "FT-SY-1800")
    attribution = order.attribution
    amounts = _amounts(attribution)

    assert Decimal(order.estimate.quoted_price) == Decimal("18.00")
    assert Decimal(order.bill.paid_amount) == Decimal("27.00")
    assert Decimal(attribution.total_delta) == Decimal("9.00")
    # 等候费 4 × 180 秒高峰 = 4 × 1.50 = 6.00
    assert amounts["WAIT_FEE"] == Decimal("6.00")
    assert len(attribution.wait_events) == 4
    assert sum(float(e.duration_s) for e in attribution.wait_events) == pytest.approx(720, abs=1)
    # 绕路增量 ≈ 1.5 公里 × 2 元/公里 = 3.00
    assert amounts["DETOUR_EXTRA"] == Decimal("3.00")
    assert len(attribution.detour_events) == 1
    assert attribution.detour_events[0].extra_distance_m == pytest.approx(1500, abs=10)
    assert amounts["MILEAGE_EXTRA"] == Decimal("0.00")
    assert amounts["RETURN_FEE"] == Decimal("0.00")
    assert amounts["NIGHT_SURCHARGE"] == Decimal("0.00")
    assert amounts["UNEXPLAINED_RESIDUAL"] == Decimal("0.00")
    assert sum(amounts.values()) == Decimal("9.00")


def test_ft_sy_1800_events_have_map_coordinates(seeded: Session) -> None:
    order = _order(seeded, "FT-SY-1800")
    attribution = order.attribution
    for event in attribution.wait_events:
        assert 41.6 < event.lat < 41.9 and 123.3 < event.lng < 123.6
        assert event.location in {"浑南中路—金阳大街", "沈阳南站", "浑南西路", "浑河桥南"}
        assert event.peak_label == "晚高峰"
        assert float(event.peak_seconds) == pytest.approx(180, abs=1)
        assert "等候费" in event.evidence_json["formula"]
    event = attribution.detour_events[0]
    assert event.entry_name == "浑南西路"
    assert event.exit_name == "浑河桥南"
    assert len(event.path_json) >= 2
    assert event.evidence_json["per_km_rate"] == "2.00"


def test_ft_sy_1800_wait_evidence_matches_spec_format(seeded: Session) -> None:
    """举证格式：路口名 + 起止时间 + 持续秒数 + 平均速度 + 高峰标签 + 沈阳费率折算金额。"""
    order = _order(seeded, "FT-SY-1800")
    evidence = order.attribution.wait_events[0].evidence_json
    for key in ("location", "time_range", "duration_s", "avg_speed_kmh", "peak_label", "fee", "formula", "coordinate"):
        assert key in evidence


# --------------------------------------------------------------------------- #
# 拥堵/等候
# --------------------------------------------------------------------------- #
def test_peak_wait_fees_per_event(seeded: Session) -> None:
    """晚高峰订单：160s→1.33、120s→1.00、90s→0.75，全部按高峰费率。"""
    order = _order(seeded, "SY-1002")
    amounts = _amounts(order.attribution)
    assert amounts["WAIT_FEE"] == Decimal("3.08")
    fees = sorted(Decimal(str(e.fee)) for e in order.attribution.wait_events)
    assert fees == [Decimal("0.75"), Decimal("1.00"), Decimal("1.33")]
    assert all(e.peak_label == "晚高峰" for e in order.attribution.wait_events)
    assert all(e.peak_seconds > 0 and e.offpeak_seconds == 0 for e in order.attribution.wait_events)
    assert amounts["DETOUR_EXTRA"] == Decimal("0.00")


# --------------------------------------------------------------------------- #
# 绕路
# --------------------------------------------------------------------------- #
def test_high_fit_orders_have_no_detour(seeded: Session) -> None:
    for order_no in ("SY-1001", "SY-1002", "SY-1006"):
        order = _order(seeded, order_no)
        assert float(order.attribution.fit_score) >= 0.8
        assert len(order.attribution.detour_events) == 0
        assert _amounts(order.attribution)["DETOUR_EXTRA"] == Decimal("0.00")


def test_low_fit_order_produces_detour_event(seeded: Session) -> None:
    order = _order(seeded, "SY-1003")
    attribution = order.attribution
    assert float(attribution.fit_score) < 0.8
    assert len(attribution.detour_events) == 1
    event = attribution.detour_events[0]
    assert event.extra_distance_m == pytest.approx(380, abs=10)
    assert Decimal(event.fee) == Decimal("0.76")  # 380m × 2元/km，与需求举证示例一致
    assert 41.6 < event.entry_lat < 41.9
    assert len(event.path_json) >= 2


# --------------------------------------------------------------------------- #
# 返空费 / 夜间档
# --------------------------------------------------------------------------- #
def test_return_fee_triggered_over_15km(seeded: Session) -> None:
    """沈阳北站 → 苏家屯 >15 公里：返空费按阶梯规则单独归因。"""
    order = _order(seeded, "SY-1006")  # 校园 → 北陵公园，全程 >15 公里
    amounts = _amounts(order.attribution)
    assert int(order.bill.actual_distance_m) > 15000
    assert amounts["RETURN_FEE"] > Decimal("0.00")
    assert int(order.estimate.est_distance_m) <= 15000
    evidence = {i.code: i for i in order.attribution.items}["RETURN_FEE"].evidence_json
    assert evidence["estimate_return_fee"] == "0.00"
    assert amounts["MILEAGE_EXTRA"] > Decimal("0.00")
    assert _conserved(order.attribution)


def test_night_surcharge_when_crossing_2200(seeded: Session) -> None:
    """21:55 上车、22:00 后结束 → 夜间/冷气档差价（起步价 9→10、运价换 455m）。"""
    order = _order(seeded, "SY-1004")
    amounts = _amounts(order.attribution)
    assert order.estimate.rule_version == "SY-DAY"
    assert order.bill.rule_version == "SY-NIGHT"
    assert amounts["NIGHT_SURCHARGE"] == Decimal("1.00")
    assert amounts["WAIT_FEE"] == Decimal("0.50")   # 90 秒平峰等候 = 90/180
    assert _conserved(order.attribution)


# --------------------------------------------------------------------------- #
# 降级与残差
# --------------------------------------------------------------------------- #
def test_missing_trajectory_degrades_and_residual_needs_review(seeded: Session) -> None:
    order = _order(seeded, "SY-1005")
    attribution = order.attribution
    amounts = _amounts(attribution)
    assert attribution.data_quality == "INCOMPLETE"
    assert attribution.status == "NEEDS_REVIEW"
    assert "TRAJECTORY_MISSING" in attribution.quality_flags_json
    assert amounts["WAIT_FEE"] == Decimal("0.00")
    assert amounts["DETOUR_EXTRA"] == Decimal("0.00")
    assert amounts["UNEXPLAINED_RESIDUAL"] == Decimal("1.50")
    residual_evidence = {i.code: i for i in attribution.items}["UNEXPLAINED_RESIDUAL"].evidence_json
    assert residual_evidence["components"]["bill_gap"] == "1.50"
    assert residual_evidence["components"]["needs_review"] is True
    assert "待核查" in {i.code: i for i in attribution.items}["UNEXPLAINED_RESIDUAL"].description
    assert _conserved(attribution)


def test_small_residual_rounds_to_zero(seeded: Session) -> None:
    """正常订单（SY-1001）差额 0：残差 0.00，无待核查。"""
    order = _order(seeded, "SY-1001")
    attribution = order.attribution
    assert Decimal(attribution.total_delta) == Decimal("0.00")
    assert Decimal(attribution.residual) == Decimal("0.00")
    assert attribution.status == "OK"


# --------------------------------------------------------------------------- #
# 确定性与幂等
# --------------------------------------------------------------------------- #
def test_attribution_is_deterministic(seeded: Session) -> None:
    order = _order(seeded, "FT-SY-1800")
    first = run_attribution(seeded, order, get_config())
    second = run_attribution(seeded, order, get_config())
    assert [str(i.amount) for i in first.items] == [str(i.amount) for i in second.items]
    assert [e.evidence["fee"] for e in first.wait_events] == [e.evidence["fee"] for e in second.wait_events]
    assert first.total_delta == second.total_delta
