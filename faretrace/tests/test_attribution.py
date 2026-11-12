"""归因核心测试：金额守恒、规则生效条件、证据、确定性、降级。"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.models import Order, PricingRule, utcnow
from app.schemas import (
    BillIn,
    EstimateIn,
    OrderCompleteIn,
    OrderCreateIn,
    ReferenceRouteIn,
    TrajectoryPointIn,
)
from app.services import geo, ingest
from app.services.attribution import run_attribution
from app.services.pricing import PricingParams, evaluate
from app.services.seed import seed_orders

EPS = Decimal("0.01")
CODES = {
    "PATH_DEVIATION",
    "CONGESTION_DELAY",
    "DRIVER_DELAY",
    "PRICING_RULE_CHANGE",
    "TOLL_OR_FEE",
    "UNEXPLAINED_RESIDUAL",
}


def _amounts(attribution) -> dict[str, Decimal]:
    return {item.code: Decimal(item.amount) for item in attribution.items}


def _conserved(attribution) -> bool:
    total = Decimal(attribution.total_delta)
    amount_sum = sum((Decimal(i.amount) for i in attribution.items), Decimal("0"))
    return abs(amount_sum - total) <= EPS


# --------------------------------------------------------------------------- #
# 种子场景
# --------------------------------------------------------------------------- #
@pytest.fixture()
def seeded(seeded_session: Session) -> Session:
    return seeded_session


def test_all_seeds_conserve_money(seeded: Session) -> None:
    orders = seeded.scalars(select(Order)).all()
    assert len(orders) == 6
    for order in orders:
        attribution = order.attribution
        assert attribution is not None
        assert _conserved(attribution), f"{order.order_no} 不守恒"
        assert set(_amounts(attribution)) == CODES
        assert Decimal(attribution.residual) == sum(
            (Decimal(i.amount) for i in attribution.items if i.code == "UNEXPLAINED_RESIDUAL"),
            Decimal("0"),
        )


def test_ft1800_exact_acceptance_numbers(seeded: Session) -> None:
    """生活化验收：18 元 → 27 元，拥堵 4.2 + 路径 3.1 + 规则 1.7 + 残差 0。"""
    order = seeded.scalars(select(Order).where(Order.order_no == "FT-1800")).one()
    attribution = order.attribution
    amounts = _amounts(attribution)

    assert Decimal(order.estimate.quoted_price) == Decimal("18.00")
    assert Decimal(order.bill.paid_amount) == Decimal("27.00")
    assert Decimal(attribution.total_delta) == Decimal("9.00")
    assert amounts["CONGESTION_DELAY"] == Decimal("4.20")
    assert amounts["PATH_DEVIATION"] == Decimal("3.10")
    assert amounts["PRICING_RULE_CHANGE"] == Decimal("1.70")
    assert amounts["DRIVER_DELAY"] == Decimal("0.00")
    assert amounts["TOLL_OR_FEE"] == Decimal("0.00")
    assert amounts["UNEXPLAINED_RESIDUAL"] == Decimal("0.00")
    assert sum(amounts.values()) == Decimal("9.00")
    assert float(attribution.fit_score) < 0.8
    assert attribution.data_quality == "COMPLETE"


def test_ft1800_evidence_contains_thresholds_and_spans(seeded: Session) -> None:
    order = seeded.scalars(select(Order).where(Order.order_no == "FT-1800")).one()
    items = {i.code: i for i in order.attribution.items}
    path_evidence = items["PATH_DEVIATION"].evidence_json
    congestion_evidence = items["CONGESTION_DELAY"].evidence_json

    assert path_evidence["thresholds"]["FIT_THRESHOLD"] == 0.8
    assert path_evidence["thresholds"]["FIT_BUFFER_M"] == 100
    assert path_evidence["fit_score"] is not None
    assert path_evidence["delta_distance_m"] == 2000
    assert path_evidence["deviation_spans"], "路径偏离必须附轨迹片段证据"

    assert congestion_evidence["thresholds"]["CONGESTION_SPEED_KMH"] == 15
    assert congestion_evidence["congestion_seconds"] == pytest.approx(840, abs=1)
    assert congestion_evidence["congestion_spans"]

    # 每项证据都必须带计算说明与规则版本（可审计）
    for item in order.attribution.items:
        assert item.evidence_json.get("explanation")
        assert item.description


def test_high_fit_means_zero_path_amount(seeded: Session) -> None:
    for order_no in ("FT-1001", "FT-1002", "FT-1004"):
        order = seeded.scalars(select(Order).where(Order.order_no == order_no)).one()
        amounts = _amounts(order.attribution)
        assert float(order.attribution.fit_score) >= 0.8
        assert amounts["PATH_DEVIATION"] == Decimal("0.00")
        evidence = {i.code: i for i in order.attribution.items}["PATH_DEVIATION"].evidence_json
        assert evidence["fit_score"] is not None
        assert evidence["avg_deviation_m"] is not None


def test_low_fit_produces_path_deviation(seeded: Session) -> None:
    order = seeded.scalars(select(Order).where(Order.order_no == "FT-1003")).one()
    amounts = _amounts(order.attribution)
    assert float(order.attribution.fit_score) < 0.8
    assert amounts["PATH_DEVIATION"] == Decimal("3.60")   # 2.00 km × 1.80 元/公里
    assert amounts["CONGESTION_DELAY"] == Decimal("1.75")  # 300 s × 0.35 元/分钟
    assert amounts["TOLL_OR_FEE"] == Decimal("0.55")


def test_congestion_seed(seeded: Session) -> None:
    order = seeded.scalars(select(Order).where(Order.order_no == "FT-1002")).one()
    amounts = _amounts(order.attribution)
    assert amounts["CONGESTION_DELAY"] == Decimal("3.50")  # 600 s × 0.35 元/分钟
    assert amounts["DRIVER_DELAY"] == Decimal("0.00")
    assert amounts["UNEXPLAINED_RESIDUAL"] == Decimal("0.00")


def test_rule_change_seed(seeded: Session) -> None:
    order = seeded.scalars(select(Order).where(Order.order_no == "FT-1004")).one()
    amounts = _amounts(order.attribution)
    assert amounts["PRICING_RULE_CHANGE"] == Decimal("3.80")  # Δ起步价 2.00 + Δ里程单价 0.30×6km
    assert amounts["CONGESTION_DELAY"] == Decimal("0.00")
    assert amounts["UNEXPLAINED_RESIDUAL"] == Decimal("0.00")
    evidence = {i.code: i for i in order.attribution.items}["PRICING_RULE_CHANGE"].evidence_json
    assert evidence["field_compare"]["base_fare"] == ["3.40", "5.40"]


def test_discount_shows_in_toll_or_fee(seeded: Session) -> None:
    order = seeded.scalars(select(Order).where(Order.order_no == "FT-1001")).one()
    amounts = _amounts(order.attribution)
    assert order.attribution.total_delta == Decimal("-0.40")
    assert amounts["TOLL_OR_FEE"] == Decimal("-0.40")
    assert amounts["UNEXPLAINED_RESIDUAL"] == Decimal("0.00")


def test_missing_trajectory_degrades_and_records_residual(seeded: Session) -> None:
    order = seeded.scalars(select(Order).where(Order.order_no == "FT-1005")).one()
    attribution = order.attribution
    amounts = _amounts(attribution)
    assert attribution.data_quality == "INCOMPLETE"
    assert attribution.status == "DEGRADED"
    assert "TRAJECTORY_MISSING" in attribution.quality_flags_json
    assert amounts["CONGESTION_DELAY"] == Decimal("0.00")
    assert amounts["DRIVER_DELAY"] == Decimal("2.10")     # 360 s × 0.35 元/分钟（降级口径）
    assert amounts["UNEXPLAINED_RESIDUAL"] == Decimal("0.20")
    residual_evidence = {i.code: i for i in attribution.items}["UNEXPLAINED_RESIDUAL"].evidence_json
    assert residual_evidence["components"]["bill_gap"] is not None
    assert _conserved(attribution)


def test_attribution_is_deterministic(seeded: Session) -> None:
    order = seeded.scalars(select(Order).where(Order.order_no == "FT-1800")).one()
    first = run_attribution(seeded, order, get_config())
    second = run_attribution(seeded, order, get_config())
    assert [str(i.amount) for i in first.items] == [str(i.amount) for i in second.items]
    assert first.total_delta == second.total_delta
    assert first.evidence_bundle["totals"] == second.evidence_bundle["totals"]


def test_reattribution_keeps_single_record_and_bumps_revision(seeded: Session) -> None:
    order = seeded.scalars(select(Order).where(Order.order_no == "FT-1800")).one()
    before = order.attribution.revision
    draft = run_attribution(seeded, order, get_config())
    ingest.persist_attribution(seeded, order, draft, get_config())
    assert order.attribution.revision == before + 1
    assert len(order.attribution.items) == 6
    assert _conserved(order.attribution)


# --------------------------------------------------------------------------- #
# 随机订单守恒（属性式测试）
# --------------------------------------------------------------------------- #
def _make_random_order(session: Session, index: int, rng: random.Random, cfg) -> Order:
    version = f"R-RAND-{index}"
    params = PricingParams(
        base_fare=Decimal(str(round(rng.uniform(2.0, 8.0), 2))),
        per_km=Decimal(str(round(rng.uniform(1.2, 3.0), 2))),
        per_min=Decimal(str(round(rng.uniform(0.2, 0.6), 2))),
        min_fare=Decimal(str(round(rng.choice([0.0, 8.0, 12.0]), 2))),
        surge_multiplier=Decimal(str(round(rng.choice([1.0, 1.0, 1.2, 1.5]), 2))),
    )
    session.add(
        PricingRule(
            version=version,
            base_fare=params.base_fare,
            per_km=params.per_km,
            per_min=params.per_min,
            min_fare=params.min_fare,
            surge_multiplier=params.surge_multiplier,
        )
    )
    session.flush()

    est_km = rng.uniform(2.0, 10.0)
    est_min = rng.uniform(8.0, 35.0)
    quoted = evaluate(params, est_km, est_min, surcharges=Decimal("0"))
    ref_meters = int(est_km * 1000)
    polyline = [
        [30.0, 120.0],
        [30.0 + ref_meters * 0.000005, 120.0 + ref_meters * 0.000008],
    ]

    create_payload = OrderCreateIn(
        order_no=f"FT-RAND-{index}",
        city="演示城市",
        estimate=EstimateIn(
            quoted_price=quoted,
            est_distance_m=ref_meters,
            est_duration_s=int(est_min * 60),
            rule_version=version,
            surge_multiplier=params.surge_multiplier,
        ),
        reference_route=ReferenceRouteIn(polyline=polyline, length_m=ref_meters),
    )
    order = ingest.create_order(session, create_payload, cfg)

    act_km = max(0.5, est_km + rng.uniform(-0.6, 2.0))
    act_min = max(1.0, est_min + rng.uniform(-3.0, 12.0))
    bill_rule = version if rng.random() < 0.5 else "R-RAND-BASE"
    if bill_rule == "R-RAND-BASE":
        session.merge(
            PricingRule(
                version="R-RAND-BASE",
                base_fare=Decimal("4.00"),
                per_km=Decimal("2.00"),
                per_min=Decimal("0.35"),
                min_fare=Decimal("0"),
                surge_multiplier=Decimal("1.00"),
            )
        )
        session.flush()

    distance_fare = Decimal(str(round(act_km * float(params.per_km), 2)))
    tobill = Decimal(str(round(act_min * float(params.per_min), 2)))
    paid = Decimal(str(round(rng.uniform(1.0, 5.0), 2)))  # 含随机未列明调整 -> 产生残差
    bill = BillIn(
        paid_amount=paid,
        actual_distance_m=int(act_km * 1000),
        actual_duration_s=int(act_min * 60),
        base_fare=params.base_fare,
        distance_fare=distance_fare,
        time_fare=tobill,
        surcharges={"过路费": "0.50"} if rng.random() < 0.3 else {},
        rule_version=bill_rule,
    )

    points: list[TrajectoryPointIn] = []
    t0 = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)
    n = rng.choice([0, 0, 12, 30])
    for k in range(n):
        frac = k / max(1, n - 1)
        lat, lng = geo.interpolate_along(polyline, ref_meters * frac)
        offset = rng.choice([0.0, 0.0, 300.0])
        lat, lng = geo.offset_point(lat, lng, 0.0, offset)
        points.append(
            TrajectoryPointIn(
                seq=k,
                ts=t0 + timedelta(seconds=int(act_min * 60 * frac)),
                lat=lat,
                lng=lng,
                speed_mps=0.0 if rng.random() < 0.2 else 5.0,
            )
        )

    complete_payload = OrderCompleteIn(bill=bill, trajectory=points)
    ingest.complete_order(session, order, complete_payload, cfg)
    return order


@pytest.mark.parametrize("seed", [101, 202, 303])
def test_random_orders_conserve_money(seed: int, session: Session, cfg) -> None:
    rng = random.Random(seed)
    orders = [_make_random_order(session, i, rng, cfg) for i in range(12)]
    for order in orders:
        attribution = order.attribution
        assert _conserved(attribution), f"{order.order_no} 不守恒: {_amounts(attribution)}"
        assert len(attribution.items) == 6
        assert (attribution.evidence_bundle_json or {})["totals"]["conservation_ok"] is True


def test_negative_total_delta_is_attributed(session: Session, cfg) -> None:
    """实付比预估低：差额为负，必须显式呈现（不能只允许正向归因）。"""
    session.merge(
        PricingRule(
            version="R-NEG-1",
            base_fare=Decimal("5.00"),
            per_km=Decimal("2.00"),
            per_min=Decimal("0.40"),
            min_fare=Decimal("0"),
            surge_multiplier=Decimal("1.00"),
        )
    )
    session.flush()
    polyline = [[30.0, 120.0], [30.0, 120.05]]
    create_payload = OrderCreateIn(
        order_no="FT-NEG-1",
        estimate=EstimateIn(
            quoted_price="21.00",
            est_distance_m=5000,
            est_duration_s=900,
            rule_version="R-NEG-1",
        ),
        reference_route=ReferenceRouteIn(polyline=polyline, length_m=5000),
    )
    order = ingest.create_order(session, create_payload, cfg)
    t0 = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)
    points = [
        TrajectoryPointIn(seq=i, ts=t0 + timedelta(seconds=60 * i), lat=30.0, lng=120.0 + 0.01 * i, speed_mps=8.0)
        for i in range(6)
    ]
    complete_payload = OrderCompleteIn(
        bill=BillIn(
            paid_amount="18.00",
            actual_distance_m=4000,
            actual_duration_s=700,
            base_fare="5.00",
            distance_fare="8.00",
            time_fare="4.67",
            discounts={"优惠券": "1.33"},
            rule_version="R-NEG-1",
        ),
        trajectory=points,
    )
    ingest.complete_order(session, order, complete_payload, cfg)
    attribution = order.attribution
    assert Decimal(attribution.total_delta) == Decimal("-3.00")
    assert _conserved(attribution)
    assert Decimal(attribution.residual) < Decimal("0.01")
