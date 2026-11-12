"""后端接口测试：错误结构、双视角账单、争议证据冻结、端到端链路。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Order, PricingRule
from app.services import geo
from tests.helpers import sample_polyline

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)


def test_health(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["config_version"] == "cfg-1.0.0"


def test_config_endpoint_is_read_only_and_exposes_thresholds(client: TestClient) -> None:
    body = client.get("/api/config").json()
    thresholds = body["thresholds"]
    assert thresholds["FIT_BUFFER_M"] == 100
    assert thresholds["FIT_THRESHOLD"] == 0.8
    assert thresholds["CONGESTION_SPEED_KMH"] == 15
    assert thresholds["MIN_CONGESTION_SECONDS"] == 60
    assert {i["code"] for i in body["attribution_items"]} == {
        "PATH_DEVIATION",
        "CONGESTION_DELAY",
        "DRIVER_DELAY",
        "PRICING_RULE_CHANGE",
        "TOLL_OR_FEE",
        "UNEXPLAINED_RESIDUAL",
    }


def test_list_orders_from_seed(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/orders").json()
    assert body["total"] == 6
    order_nos = {o["order_no"] for o in body["items"]}
    assert "FT-1800" in order_nos
    ft1800 = next(o for o in body["items"] if o["order_no"] == "FT-1800")
    assert ft1800["quoted_price"] == "18.00"
    assert ft1800["paid_amount"] == "27.00"
    assert ft1800["total_delta"] == "9.00"


def test_bill_views_passenger_and_driver(seeded_client: TestClient) -> None:
    both = seeded_client.get("/api/orders/FT-1800/bill").json()
    passenger, driver = both["passenger"], both["driver"]
    assert passenger["role"] == "passenger"
    assert driver["role"] == "driver"
    assert len(passenger["items"]) == 6
    assert len(driver["items"]) == 6
    assert passenger["conservation"]["ok"] is True
    assert passenger["summary"]["paid_amount"] == "27.00"
    # 司机视角分组：拥堵属路况，规则属平台
    groups = driver["summary"]["groups"]
    assert groups["road_condition"]["amount"] == "4.20"
    assert groups["driver_related"]["amount"] == "3.10"
    assert groups["platform_rule"]["amount"] == "1.70"

    alone = seeded_client.get("/api/orders/FT-1800/bill", params={"view": "driver"}).json()
    assert alone["bill"]["role"] == "driver"


def test_bill_degraded_notice_for_missing_trajectory(seeded_client: TestClient) -> None:
    payload = seeded_client.get("/api/orders/FT-1005/bill").json()["passenger"]
    assert payload["data_quality"] == "INCOMPLETE"
    assert "不完整轨迹" in payload["quality_notice"]
    assert payload["residual"] == 0.2


def test_attribution_raw_endpoint_has_evidence_bundle(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/orders/FT-1800/attribution").json()
    assert body["total_delta"] == "9.00"
    assert body["evidence_bundle"]["totals"]["conservation_ok"] is True
    assert body["fit_score"] < 0.8
    items = {i["code"]: i for i in body["items"]}
    assert items["PATH_DEVIATION"]["evidence"]["thresholds"]["FIT_THRESHOLD"] == 0.8
    assert items["CONGESTION_DELAY"]["evidence"]["congestion_seconds"] == pytest.approx(840, abs=1)


def test_dispute_flow_binds_evidence_and_locks_snapshot(seeded_client: TestClient) -> None:
    create = seeded_client.post(
        "/api/disputes",
        json={
            "order_no": "FT-1800",
            "item_code": "PATH_DEVIATION",
            "initiator_role": "passenger",
            "reason": "这段路我经常走，不应该多 2 公里",
        },
    )
    assert create.status_code == 201
    dispute = create.json()
    assert dispute["status"] == "OPEN"
    assert dispute["snapshot_locked"] is True
    assert dispute["evidence_count"] == 1
    snapshot = dispute["evidence_snapshot"]
    assert snapshot["focused_item"]["amount"] == "3.10"
    assert len(snapshot["items"]) == 6
    assert snapshot["raw"]["bill"]["paid_amount"] == "27.00"
    assert dispute["evidence_bindings"][0]["evidence"]["item"]["code"] == "PATH_DEVIATION"

    # 重算归因后，争议快照必须保持不变（revision 不同）
    reattr = seeded_client.post("/api/orders/FT-1800/attribute")
    assert reattr.status_code == 200
    new_revision = reattr.json()["attribution"]["revision"]
    fetched = seeded_client.get(f"/api/disputes/{dispute['id']}").json()
    assert fetched["evidence_snapshot"]["attribution"]["revision"] < new_revision
    assert fetched["evidence_snapshot"]["focused_item"]["amount"] == "3.10"

    # 状态机：OPEN -> REVIEWING -> RESOLVED；非法流转被拒
    r1 = seeded_client.patch(f"/api/disputes/{dispute['id']}", json={"status": "REVIEWING", "reviewed_by": "运营-张"})
    assert r1.status_code == 200 and r1.json()["status"] == "REVIEWING"
    bad = seeded_client.patch(f"/api/disputes/{dispute['id']}", json={"status": "OPEN"})
    assert bad.status_code == 409
    assert bad.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
    r2 = seeded_client.patch(
        f"/api/disputes/{dispute['id']}",
        json={"status": "RESOLVED", "resolution": "轨迹证据成立，给出 3.10 元说明"},
    )
    assert r2.json()["status"] == "RESOLVED"
    assert r2.json()["resolution"]

    listing = seeded_client.get("/api/disputes", params={"order_no": "FT-1800"}).json()
    assert listing["total"] == 1


def test_dispute_rejects_unknown_item_and_role(seeded_client: TestClient) -> None:
    resp = seeded_client.post(
        "/api/disputes",
        json={"order_no": "FT-1800", "item_code": "NOPE", "initiator_role": "passenger", "reason": "x"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    assert "NOPE" in resp.json()["error"]["message"]

    resp2 = seeded_client.post(
        "/api/disputes",
        json={"order_no": "FT-1800", "item_code": "PATH_DEVIATION", "initiator_role": "boss", "reason": "x"},
    )
    assert resp2.status_code == 422


def test_unified_error_structure(client: TestClient) -> None:
    resp = client.get("/api/orders/NOT-EXISTS")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "NOT_FOUND"
    assert "NOT-EXISTS" in body["error"]["message"]

    invalid = client.post("/api/orders", json={"estimate": {}})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"


def test_e2e_create_complete_bill_dispute(seeded_client: TestClient) -> None:
    polyline = sample_polyline(meters=5000)
    order_no = "FT-E2E-1"
    create = seeded_client.post(
        "/api/orders",
        json={
            "order_no": order_no,
            "city": "演示城市",
            "origin_label": "E2E 起点",
            "destination_label": "E2E 终点",
            "estimate": {
                "quoted_price": "21.00",
                "est_distance_m": 5000,
                "est_duration_s": 900,
                "rule_version": "R-EST-1",
                "surge_multiplier": "1.00",
            },
            "reference_route": {"polyline": polyline, "length_m": 5000},
        },
    )
    assert create.status_code == 201, create.text

    # 沿参考路径行驶 + 中途 120 秒拥堵
    points = []
    total = geo.polyline_length_m(polyline)
    for i in range(21):
        frac = i / 20
        lat, lng = geo.interpolate_along(polyline, total * frac)
        points.append({"seq": i, "ts": (T0 + timedelta(seconds=40 * i)).isoformat(), "lat": lat, "lng": lng, "speed_mps": 6.0})
    for j in range(6):
        lat, lng = geo.interpolate_along(polyline, total * 0.5)
        points.append({"seq": 21 + j, "ts": (T0 + timedelta(seconds=800 + 20 * j)).isoformat(), "lat": lat, "lng": lng, "speed_mps": 0.0})

    complete = seeded_client.post(
        f"/api/orders/{order_no}/complete",
        json={
            "bill": {
                "paid_amount": "23.50",
                "actual_distance_m": 5000,
                "actual_duration_s": 1400,
                "base_fare": "5.00",
                "distance_fare": "10.00",
                "time_fare": "8.50",
                "rule_version": "R-EST-1",
                "surcharges": {},
                "other_fees": {},
                "discounts": {},
            },
            "trajectory": points,
        },
    )
    assert complete.status_code == 201, complete.text
    attribution = complete.json()["attribution"]
    assert Decimal(attribution["total_delta"]) == Decimal("2.50")
    assert attribution["evidence_bundle"]["totals"]["conservation_ok"] is True

    bill = seeded_client.get(f"/api/orders/{order_no}/bill").json()
    assert bill["passenger"]["conservation"]["ok"] is True
    assert len(bill["passenger"]["items"]) == 6
    assert bill["driver"]["role"] == "driver"

    # 重复完成 -> 409
    duplicate = seeded_client.post(f"/api/orders/{order_no}/complete", json={"bill": {}, "trajectory": []})
    assert duplicate.status_code == 422 or duplicate.status_code == 409

    dispute = seeded_client.post(
        "/api/disputes",
        json={
            "order_no": order_no,
            "item_code": "CONGESTION_DELAY",
            "initiator_role": "driver",
            "reason": "当时前方事故封路，不属于我慢",
        },
    )
    assert dispute.status_code == 201
    assert dispute.json()["evidence_snapshot"]["raw"]["trajectory"]["point_count"] > 0


def test_demo_reset_reloads_seeds(seeded_client: TestClient) -> None:
    before = seeded_client.get("/api/orders").json()["total"]
    resp = seeded_client.post("/api/demo/reset")
    assert resp.status_code == 200
    assert len(resp.json()["seeded_orders"]) == 6
    after = seeded_client.get("/api/orders").json()["total"]
    assert after == before == 6


def test_missing_pricing_rule_is_reported(client: TestClient) -> None:
    resp = client.post(
        "/api/orders",
        json={
            "order_no": "FT-NO-RULE",
            "estimate": {
                "quoted_price": "10.00",
                "est_distance_m": 1000,
                "est_duration_s": 300,
                "rule_version": "R-UNKNOWN",
            },
            "reference_route": {"polyline": sample_polyline(meters=1000)},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
