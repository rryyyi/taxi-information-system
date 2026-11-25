"""前端页面冒烟测试（订单列表 / 透明账单+Leaflet 地图 / 争议提交）。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_orders_page_lists_seed_orders(seeded_client: TestClient) -> None:
    resp = seeded_client.get("/orders")
    assert resp.status_code == 200
    html = resp.text
    assert "订单列表" in html
    assert "FT-SY-1800" in html
    assert "待核实差额" in html
    assert ">NEEDS_REVIEW<" not in html
    assert "curl -X" not in html


def test_bill_page_renders_views_and_leaflet_map(seeded_client: TestClient) -> None:
    resp = seeded_client.get("/orders/FT-SY-1800/bill")
    assert resp.status_code == 200
    html = resp.text
    assert "透明账单" in html
    assert "乘客视角" in html and "司机视角" in html
    assert "等候费" in html
    assert "绕路增加的费用" in html
    assert "适用规则" in html and "本单怎么算" in html
    assert "高峰 180.0 秒 ÷ 120" in html
    assert "1500.8 米 ÷ 1000 × 2.00" in html
    assert "展开 JSON" not in html and "<pre>" not in html
    assert "残差" in html
    assert "金额守恒校验" in html
    assert "通过（±0.01 元内）" in html
    # Leaflet + OSM 底图
    assert 'id="map"' in html
    assert "leaflet" in html.lower()
    assert "openstreetmap" in html.lower()
    # 地图数据已内嵌（实际轨迹/等候事件/绕路段坐标）
    assert "wait_events" in html
    assert "detour_segments" in html
    assert "123.43" in html


def test_bill_page_warns_on_incomplete_trajectory(seeded_client: TestClient) -> None:
    resp = seeded_client.get("/orders/SY-1005/bill")
    assert resp.status_code == 200
    assert "基于不完整轨迹生成" in resp.text
    assert "地图只展示已知起终点" in resp.text


def test_dispute_form_page(seeded_client: TestClient) -> None:
    resp = seeded_client.get("/disputes/new")
    assert resp.status_code == 200
    html = resp.text
    assert "争议提交" in html
    assert 'name="item_code"' in html
    assert "FT-SY-1800" in html


def test_dispute_form_submission_redirects_to_bill(seeded_client: TestClient) -> None:
    resp = seeded_client.post(
        "/disputes",
        data={
            "order_no": "FT-SY-1800",
            "item_code": "DETOUR_EXTRA",
            "initiator_role": "passenger",
            "reason": "页面表单提交测试",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "已提交争议" in resp.text
    recent = seeded_client.get("/disputes/new").text
    assert "已受理" in recent
    assert ">OPEN<" not in recent
    assert ">passenger<" not in recent
    assert ">DETOUR_EXTRA<" not in recent


def test_static_css_served(seeded_client: TestClient) -> None:
    assert seeded_client.get("/static/style.css").status_code == 200


def test_home_redirects_to_orders(seeded_client: TestClient) -> None:
    resp = seeded_client.get("/", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == "/orders"
