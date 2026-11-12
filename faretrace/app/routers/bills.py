"""透明账单接口：同一份归因，乘客视角 / 司机视角。"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_session
from app.errors import ConflictError
from app.services import ingest

router = APIRouter(prefix="/api", tags=["bills"])


@router.get("/orders/{order_no}/bill")
def get_bill(
    order_no: str,
    view: Literal["passenger", "driver", "both"] = Query(default="both"),
    session: Session = Depends(get_session),
) -> dict:
    order = ingest.get_order(session, order_no)
    attribution = order.attribution
    if attribution is None or attribution.transparent_bill is None:
        raise ConflictError("订单尚未生成透明账单", details={"order_no": order_no})
    tb = attribution.transparent_bill
    if view == "passenger":
        return {"order_no": order.order_no, "bill": tb.passenger_view_json}
    if view == "driver":
        return {"order_no": order.order_no, "bill": tb.driver_view_json}
    return {
        "order_no": order.order_no,
        "passenger": tb.passenger_view_json,
        "driver": tb.driver_view_json,
    }
