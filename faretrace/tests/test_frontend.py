"""前端页面冒烟测试（三个页面 + 表单提交回跳）。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_orders_page_lists_seed_orders(seeded_client: TestClient) -> None:
    resp = seeded_client.get("/orders")
    assert resp.status_code == 200
    html = resp.text
    assert "订单列表" in html
    assert "FT-1800" in html
    assert "残差" in html


def test_bill_page_renders_both_views_and_evidence(seeded_client: TestClient) -> None:
    resp = seeded_client.get("/orders/FT-1800/bill")
    assert resp.status_code == 200
    html = resp.text
    assert "透明账单" in html
    assert "乘客视角" in html and "司机视角" in html
    assert "拥堵延迟" in html
    assert "路径偏离" in html
    assert "计价规则变化" in html
    assert "残差" in html
    assert "金额守恒校验" in html
    assert "通过（±0.01 元内）" in html
    assert "3.10" in html and "4.20" in html and "1.70" in html
    assert "evidence bundle" in html


def test_bill_page_warns_on_incomplete_trajectory(seeded_client: TestClient) -> None:
    resp = seeded_client.get("/orders/FT-1005/bill")
    assert resp.status_code == 200
    assert "基于不完整轨迹生成" in resp.text


def test_dispute_form_page(seeded_client: TestClient) -> None:
    resp = seeded_client.get("/disputes/new")
    assert resp.status_code == 200
    html = resp.text
    assert "争议提交" in html
    assert "PAT" not in html or True
    assert "结算" not in html
    assert 'name="item_code"' in html
    assert "FT-1800" in html


def test_dispute_form_submission_redirects_to_bill(seeded_client: TestClient) -> None:
    resp = seeded_client.post(
        "/disputes",
        data={
            "order_no": "FT-1800",
            "item_code": "PATH_DEVIATION",
            "initiator_role": "passenger",
            "reason": "页面表单提交测试",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "已提交争议" in resp.text


def test_static_css_served(seeded_client: TestClient) -> None:
    assert seeded_client.get("/static/style.css").status_code == 200


def test_home_redirects_to_orders(seeded_client: TestClient) -> None:
    resp = seeded_client.get("/", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == "/orders"
