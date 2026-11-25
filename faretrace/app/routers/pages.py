"""三个前端页面（订单列表 / 透明账单详情 / 争议提交），Jinja2 服务端渲染，无前端工程化。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.errors import AppError
from app.models import Attribution, Dispute, Order
from app.services import disputes as dispute_service
from app.services import ingest
from app.services.presentation import chinese, readable_items

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
router = APIRouter(include_in_schema=False)


TEMPLATES.env.filters["chinese"] = chinese


@router.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    return RedirectResponse(url="/orders")


@router.get("/orders", response_class=HTMLResponse, include_in_schema=False)
def orders_page(request: Request, session: Session = Depends(get_session)):
    orders = list(session.scalars(select(Order).order_by(Order.created_at.desc())).all())
    rows = []
    for order in orders:
        attribution = order.attribution
        rows.append(
            {
                "order_no": order.order_no,
                "origin": order.origin_label or "-",
                "destination": order.destination_label or "-",
                "quoted": f"{order.estimate.quoted_price:.2f}" if order.estimate else "-",
                "paid": f"{order.bill.paid_amount:.2f}" if order.bill else "-",
                "delta": f"{attribution.total_delta:.2f}" if attribution else "-",
                "residual": f"{attribution.residual:.2f}" if attribution else "-",
                "fit": f"{float(attribution.fit_score):.2f}" if attribution and attribution.fit_score is not None else "-",
                "quality": chinese(attribution.data_quality) if attribution else "等待行程结束",
                "status": chinese(attribution.status if attribution else order.status),
                "review": bool(attribution and attribution.residual != 0),
                "date": order.requested_at.strftime("%m月%d日 %H:%M") if order.requested_at else "",
                "complete": bool(attribution and attribution.data_quality == "COMPLETE"),
            }
        )
    return TEMPLATES.TemplateResponse(
        request, "orders.html", {"active": "orders", "rows": rows,
            "stats": {"total": len(rows), "review": sum(r["review"] for r in rows),
                      "complete": sum(r["complete"] for r in rows)}}
    )


@router.get("/orders/{order_no}/bill", response_class=HTMLResponse, include_in_schema=False)
def bill_page(request: Request, order_no: str, session: Session = Depends(get_session)):
    order = ingest.get_order(session, order_no)
    attribution = order.attribution
    views = None
    items = []
    if attribution and attribution.transparent_bill:
        tb = attribution.transparent_bill
        views = {"passenger": tb.passenger_view_json, "driver": tb.driver_view_json}

    # 页面只整理同一份已保存证据，金额计算仍由归因服务负责。
    calculations = readable_items(attribution) if attribution else []

    return TEMPLATES.TemplateResponse(
        request,
        "bill.html",
        {
            "active": "orders",
            "order": order,
            "attribution": attribution,
            "views": views,
            "items": attribution.items if attribution else [],
            "calculations": calculations,
            "start_point": order.reference_route.polyline_json[0] if order.reference_route and order.reference_route.polyline_json else None,
            "end_point": order.reference_route.polyline_json[-1] if order.reference_route and order.reference_route.polyline_json else None,
            "quality_flags": list(attribution.quality_flags_json or []) if attribution else [],
            "dispute_created": request.query_params.get("dispute"),
        },
    )


@router.get("/disputes/new", response_class=HTMLResponse, include_in_schema=False)
def dispute_form(
    request: Request,
    order_no: str | None = None,
    item_code: str | None = None,
    session: Session = Depends(get_session),
):
    attributed = list(
        session.scalars(
            select(Order)
            .join(Attribution, Attribution.order_id == Order.id)
            .order_by(Order.created_at.desc())
        ).all()
    )
    orders = []
    for order in attributed:
        orders.append(
            {
                "order_no": order.order_no,
                "item_list": [{"code": i.code, "label": i.label, "amount": f"{i.amount:.2f}"} for i in order.attribution.items],
            }
        )
    disputes = list(
        session.scalars(select(Dispute).order_by(Dispute.created_at.desc()).limit(20)).all()
    )
    return TEMPLATES.TemplateResponse(
        request,
        "dispute_form.html",
        {
            "active": "disputes",
            "orders": orders,
            "preselected_order": order_no,
            "preselected_item": item_code,
            "disputes": [
                {
                    "id": d.id,
                    "order_no": d.order_no,
                    "item_code": d.item_code,
                    "role": d.initiator_role,
                    "status": d.status,
                    "reason": d.reason,
                    "created_at": d.created_at.strftime("%Y-%m-%d %H:%M") if d.created_at else "",
                }
                for d in disputes
            ],
        },
    )


@router.post("/disputes", include_in_schema=False)
def submit_dispute_form(
    request: Request,
    order_no: str = Form(...),
    item_code: str = Form(...),
    initiator_role: str = Form(...),
    reason: str = Form(""),
    session: Session = Depends(get_session),
):
    try:
        dispute = dispute_service.create_dispute(
            session,
            order_no=order_no,
            item_code=item_code,
            initiator_role=initiator_role,
            reason=reason,
        )
        session.commit()
        return RedirectResponse(url=f"/orders/{order_no}/bill?dispute={dispute.id}", status_code=303)
    except AppError as exc:
        session.rollback()
        attributed = list(
            session.scalars(
                select(Order)
                .join(Attribution, Attribution.order_id == Order.id)
                .order_by(Order.created_at.desc())
            ).all()
        )
        orders = []
        for order in attributed:
            orders.append(
                {
                    "order_no": order.order_no,
                    "item_list": [
                        {"code": i.code, "label": i.label, "amount": f"{i.amount:.2f}"}
                        for i in order.attribution.items
                    ],
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "dispute_form.html",
            {
                "active": "disputes",
                "orders": orders,
                "preselected_order": order_no,
                "preselected_item": item_code,
                "disputes": [],
                "error": exc.message,
                "reason": reason,
                "selected_role": initiator_role,
            },
            status_code=400,
        )
