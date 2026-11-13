"""订单接口：创建（含预估价快照与参考路径）、完成（含账单与轨迹，触发归因）、查询。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import get_session
from app.errors import ConflictError, ValidationError
from app.models import Order
from app.routers.serializers import attribution_detail, order_detail, order_summary
from app.schemas import OrderCompleteIn, OrderCreateIn
from app.services import ingest
from app.services.attribution import AttributionError, run_attribution

router = APIRouter(prefix="/api", tags=["orders"])


@router.post("/orders", status_code=201)
def create_order(payload: OrderCreateIn, session: Session = Depends(get_session)) -> dict:
    cfg = get_config()
    order = ingest.create_order(session, payload, cfg)
    return order_detail(order)


@router.post("/orders/{order_no}/complete", status_code=201)
def complete_order(
    order_no: str, payload: OrderCompleteIn, session: Session = Depends(get_session)
) -> dict:
    cfg = get_config()
    order = ingest.get_order(session, order_no)
    try:
        attribution = ingest.complete_order(session, order, payload, cfg)
    except AttributionError as exc:
        raise ValidationError(str(exc), code=exc.code) from exc
    return {
        "order_no": order.order_no,
        "attribution": attribution_detail(attribution),
        "bill": {
            "passenger": attribution.transparent_bill.passenger_view_json,
            "driver": attribution.transparent_bill.driver_view_json,
        },
    }


@router.post("/orders/{order_no}/attribute", status_code=200)
def reattribute(order_no: str, session: Session = Depends(get_session)) -> dict:
    """重算归因（幂等：revision+1，已冻结的争议快照不受影响）。"""
    cfg = get_config()
    order = ingest.get_order(session, order_no)
    if order.bill is None:
        raise ConflictError("订单尚未完成，无法归因", details={"order_no": order_no})
    try:
        draft = run_attribution(session, order, cfg)
    except AttributionError as exc:
        raise ValidationError(str(exc), code=exc.code) from exc
    attribution = ingest.persist_attribution(session, order, draft, cfg)
    return {
        "order_no": order.order_no,
        "attribution": attribution_detail(attribution),
    }


@router.get("/orders")
def list_orders(
    limit: int = Query(default=50, ge=1, le=200, description="返回条数上限"),
    session: Session = Depends(get_session),
) -> dict:
    orders = list(
        session.scalars(select(Order).order_by(Order.created_at.desc()).limit(limit)).all()
    )
    return {"total": len(orders), "items": [order_summary(o) for o in orders]}


@router.get("/orders/{order_no}")
def get_order(order_no: str, session: Session = Depends(get_session)) -> dict:
    order = ingest.get_order(session, order_no)
    return order_detail(order)


@router.get("/orders/{order_no}/attribution")
def get_attribution(order_no: str, session: Session = Depends(get_session)) -> dict:
    """原始归因记录（含每项证据 JSON 与完整 evidence bundle）。"""
    order = ingest.get_order(session, order_no)
    if order.attribution is None:
        raise ConflictError("订单尚未完成归因", details={"order_no": order_no})
    return attribution_detail(order.attribution)


@router.get("/orders/{order_no}/events")
def get_events(order_no: str, session: Session = Depends(get_session)) -> dict:
    """等候事件与绕路事件列表（含地图坐标，供 Leaflet 定位）。"""
    order = ingest.get_order(session, order_no)
    attribution = order.attribution
    if attribution is None:
        raise ConflictError("订单尚未完成归因", details={"order_no": order_no})
    return {
        "order_no": order.order_no,
        "data_quality": attribution.data_quality,
        "wait_events": [
            {
                "id": e.id,
                "location": e.location,
                "t_start": e.t_start.isoformat() if e.t_start else None,
                "t_end": e.t_end.isoformat() if e.t_end else None,
                "duration_s": float(e.duration_s),
                "avg_speed_kmh": float(e.avg_speed_kmh),
                "peak_seconds": float(e.peak_seconds),
                "offpeak_seconds": float(e.offpeak_seconds),
                "peak_label": e.peak_label,
                "fee": f"{e.fee:.2f}",
                "coordinate": {"lat": float(e.lat), "lng": float(e.lng)},
                "evidence": e.evidence_json,
            }
            for e in attribution.wait_events
        ],
        "detour_events": [
            {
                "id": e.id,
                "entry_name": e.entry_name,
                "exit_name": e.exit_name,
                "entry": {"lat": float(e.entry_lat), "lng": float(e.entry_lng)},
                "exit": {"lat": float(e.exit_lat), "lng": float(e.exit_lng)},
                "ref_distance_m": float(e.ref_distance_m),
                "actual_distance_m": float(e.actual_distance_m),
                "extra_distance_m": float(e.extra_distance_m),
                "fee": f"{e.fee:.2f}",
                "path": [list(p) for p in (e.path_json or [])],
                "evidence": e.evidence_json,
            }
            for e in attribution.detour_events
        ],
    }
