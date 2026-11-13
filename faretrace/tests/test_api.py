"""后端接口测试：错误结构、双视角账单、等候/绕路事件、争议冻结、端到端（沈阳版）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.services import geo, roadnet
from tests.helpers import complete_payload, order_payload, sample_polyline

T0 = datetime(2026, 3, 6, 10, 0, 0)


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["city"] == "沈阳"
    assert body["config_version"] == "cfg-sy-1.0.0"


def test_config_exposes_shenyang_fare_and_thresholds(client: TestClient) -> None:
    body = client.get("/api/config").json()
    fare = body["shenyang_fare"]
    assert fare["DAY"]["base_fare"] == 9.00
    assert fare["DAY"]["meter_step_m"] == 500
    assert fare["NIGHT"]["base_fare"] == 10.00
    assert fare["NIGHT"]["meter_step_m"] == 455
    assert fare["wait_fee"]["peak_seconds_per_yuan"] == 120
    assert "07:00-09:00" in fare["peak_windows"]
    assert body["thresholds"]["SPEED_THRESHOLD_KMH"] == 12.0
    assert {i["code"] for i in body["attribution_items"]} == {
        "WAIT_FEE", "DETOUR_EXTRA", "MILEAGE_EXTRA", "RETURN_FEE",
        "NIGHT_SURCHARGE", "PRICING_RULE_CHANGE", "UNEXPLAINED_RESIDUAL",
    }


def test_orders_list_contains_seeds(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/orders").json()
    order_nos = {o["order_no"] for o in body["items"]}
    assert "FT-SY-1800" in order_nos and "SY-1006" in order_nos
    ft = next(o for o in body["items"] if o["order_no"] == "FT-SY-1800")
    assert ft["quoted_price"] == "18.00"
    assert ft["paid_amount"] == "27.00"
    assert ft["total_delta"] == "9.00"
    assert ft["wait_event_count"] == 4
    assert ft["detour_event_count"] == 1


def test_bill_views_with_map_data(seeded_client: TestClient) -> None:
    both = seeded_client.get("/api/orders/FT-SY-1800/bill").json()
    passenger, driver = both["passenger"], both["driver"]
    assert passenger["conservation"]["ok"] is True
    assert passenger["summary"]["narrative"].count("元") >= 2

    # 地图数据：实际轨迹、参考路径、等候事件坐标、绕路段高亮
    map_data = passenger["map"]
    assert map_data["center"] == [41.80, 123.43]
    assert len(map_data["actual_path"]) > 10
    assert len(map_data["reference_path"]) > 10
    assert len(map_data["wait_events"]) == 4
    assert all(41.6 < e["lat"] < 41.9 for e in map_data["wait_events"])
    assert len(map_data["detour_segments"]) == 1
    seg = map_data["detour_segments"][0]
    assert seg["entry_name"] == "青年大街—文艺路"
    assert len(seg["path"]) >= 2

    groups = driver["summary"]["groups"]
    assert groups["road_condition"]["amount"] == "6.00"
    assert groups["driver_related"]["amount"] == "3.00"

    alone = seeded_client.get("/api/orders/FT-SY-1800/bill", params={"view": "driver"}).json()
    assert alone["bill"]["role"] == "driver"


def test_bill_degraded_notice(seeded_client: TestClient) -> None:
    payload = seeded_client.get("/api/orders/SY-1005/bill").json()["passenger"]
    assert payload["data_quality"] == "INCOMPLETE"
    assert "不完整轨迹" in payload["quality_notice"]
    assert payload["residual"] == 1.5


def test_events_endpoint_with_coordinates(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/orders/FT-SY-1800/events").json()
    assert body["order_no"] == "FT-SY-1800"
    assert len(body["wait_events"]) == 4
    first = body["wait_events"][0]
    assert first["location"].startswith("青年大街")
    assert first["peak_label"] == "晚高峰"
    assert first["fee"] == "1.50"
    assert 41.6 < first["coordinate"]["lat"] < 41.9

    assert len(body["detour_events"]) == 1
    detour = body["detour_events"][0]
    assert detour["entry_name"] == "青年大街—文艺路"
    assert detour["extra_distance_m"] == pytest.approx(1500, abs=10)
    assert detour["fee"] == "3.00"
    assert len(detour["path"]) >= 2

    empty = seeded_client.get("/api/orders/SY-1001/events").json()
    assert empty["wait_events"] == [] and empty["detour_events"] == []


def test_attribution_raw_endpoint(seeded_client: TestClient) -> None:
    body = seeded_client.get("/api/orders/FT-SY-1800/attribution").json()
    assert body["total_delta"] == "9.00"
    assert body["evidence_bundle"]["totals"]["conservation_ok"] is True
    assert body["evidence_bundle"]["fare_basis"]["DAY"]["base_fare"] == 9.00
    items = {i["code"]: i for i in body["items"]}
    assert items["WAIT_FEE"]["evidence"]["event_count"] == 4
    assert items["PRICING_RULE_CHANGE"]["amount"] == "0.00"


def test_dispute_flow_binds_evidence_and_locks_snapshot(seeded_client: TestClient) -> None:
    create = seeded_client.post(
        "/api/disputes",
        json={
            "order_no": "FT-SY-1800",
            "item_code": "DETOUR_EXTRA",
            "initiator_role": "passenger",
            "reason": "文艺路到科普路不可能多走 1.5 公里",
        },
    )
    assert create.status_code == 201
    dispute = create.json()
    assert dispute["status"] == "OPEN"
    assert dispute["snapshot_locked"] is True
    assert dispute["evidence_count"] == 1
    snapshot = dispute["evidence_snapshot"]
    assert snapshot["focused_item"]["amount"] == "3.00"
    assert len(snapshot["items"]) == 7
    assert snapshot["raw"]["bill"]["paid_amount"] == "27.00"
    assert dispute["evidence_bindings"][0]["evidence"]["item"]["code"] == "DETOUR_EXTRA"

    reattr = seeded_client.post("/api/orders/FT-SY-1800/attribute")
    new_revision = reattr.json()["attribution"]["revision"]
    fetched = seeded_client.get(f"/api/disputes/{dispute['id']}").json()
    assert fetched["evidence_snapshot"]["attribution"]["revision"] < new_revision
    assert fetched["evidence_snapshot"]["focused_item"]["amount"] == "3.00"

    r1 = seeded_client.patch(f"/api/disputes/{dispute['id']}", json={"status": "REVIEWING", "reviewed_by": "运营-沈阳"})
    assert r1.json()["status"] == "REVIEWING"
    bad = seeded_client.patch(f"/api/disputes/{dispute['id']}", json={"status": "OPEN"})
    assert bad.status_code == 409
    assert bad.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
    r2 = seeded_client.patch(f"/api/disputes/{dispute['id']}", json={"status": "RESOLVED", "resolution": "轨迹证据成立"})
    assert r2.json()["status"] == "RESOLVED"


def test_unified_error_structure(client: TestClient) -> None:
    resp = client.get("/api/orders/NOT-EXISTS")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "NOT_FOUND"

    invalid = client.post("/api/orders", json={"estimate": {}})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"


def test_e2e_create_complete_bill_events_dispute(seeded_client: TestClient) -> None:
    """创建订单 → 完成订单 → 查询透明账单 → 查看等候/绕路事件 → 提交争议。"""
    ref = roadnet.polyline_from_names(["市府广场", "青年大街—市府大路", "青年大街—十一纬路", "青年大街—文艺路"])
    order_no = "SY-E2E-1"
    create = seeded_client.post(
        "/api/orders",
        json={
            "order_no": order_no,
            "origin_label": "市府广场",
            "destination_label": "青年大街—文艺路",
            "requested_at": "2026-03-06T17:00:00",
            "estimate": {
                "quoted_price": "11.00",
                "est_distance_m": int(round(ref.length_m)),
                "est_duration_s": 300,
            },
            "reference_route": {
                "polyline": [[p[0], p[1]] for p in ref.points],
                "length_m": int(round(ref.length_m)),
                "node_names": [n["name"] for n in ref.nodes],
            },
        },
    )
    assert create.status_code == 201, create.text

    pts = roadnet.drive(
        ref.points,
        datetime(2026, 3, 6, 17, 0, 0),
        speed_mps=11.0,
        stops=[("青年大街—十一纬路", 120)],
        node_index_by_name={n["name"]: n["index"] for n in ref.nodes},
    )
    duration = int((pts[-1]["ts"] - pts[0]["ts"]).total_seconds())
    complete = seeded_client.post(
        f"/api/orders/{order_no}/complete",
        json=complete_payload(
            paid="12.00",  # 9 + 2(里程) + 1.00(120s 高峰等候)
            actual_distance_m=int(round(ref.length_m)),
            actual_duration_s=duration,
            points=[
                {"seq": i, "ts": p["ts"].isoformat(), "lat": p["lat"], "lng": p["lng"], "speed_mps": p["speed_mps"]}
                for i, p in enumerate(pts)
            ],
            completed_at="2026-03-06T17:06:00",
        ),
    )
    assert complete.status_code == 201, complete.text
    attribution = complete.json()["attribution"]
    assert attribution["evidence_bundle"]["totals"]["conservation_ok"] is True

    events = seeded_client.get(f"/api/orders/{order_no}/events").json()
    assert len(events["wait_events"]) == 1
    assert events["wait_events"][0]["duration_s"] == pytest.approx(120, abs=1)
    assert events["wait_events"][0]["peak_label"] == "晚高峰"

    dispute = seeded_client.post(
        "/api/disputes",
        json={
            "order_no": order_no,
            "item_code": "WAIT_FEE",
            "initiator_role": "driver",
            "reason": "十一纬路口红灯排队，属正常等候",
        },
    )
    assert dispute.status_code == 201
    assert dispute.json()["evidence_snapshot"]["focused_item"]["code"] == "WAIT_FEE"


def test_night_rule_auto_detected_in_api(seeded_client: TestClient) -> None:
    """23:00 完成的订单自动按夜间档计费（档位由沈阳规则按时间判定）。"""
    ref = roadnet.polyline_from_names(["沈阳站", "市府广场"])
    create = seeded_client.post(
        "/api/orders",
        json={
            "order_no": "SY-NIGHT-API",
            "requested_at": "2026-03-06T22:30:00",
            "estimate": {
                "quoted_price": "11.00",
                "est_distance_m": int(round(ref.length_m)),
                "est_duration_s": 300,
            },
            "reference_route": {"polyline": [[p[0], p[1]] for p in ref.points], "length_m": int(round(ref.length_m))},
        },
    )
    assert create.status_code == 201
    detail = seeded_client.get("/api/orders/SY-NIGHT-API").json()
    assert detail["estimate_snapshot"]["rule_version"] == "SY-NIGHT"


def test_demo_reset_reloads_seeds(seeded_client: TestClient) -> None:
    resp = seeded_client.post("/api/demo/reset")
    assert resp.status_code == 200
    assert len(resp.json()["seeded_orders"]) == 7
    assert seeded_client.get("/api/orders").json()["total"] == 7
