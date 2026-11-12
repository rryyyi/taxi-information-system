"""争议接口：提交（冻结证据快照）、查询、运营复核流转。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_session
from app.enums import DisputeStatus
from app.schemas import DisputeCreateIn, DisputeUpdateIn
from app.services import disputes as dispute_service

router = APIRouter(prefix="/api", tags=["disputes"])


@router.post("/disputes", status_code=201)
def create_dispute(
    payload: DisputeCreateIn, session: Session = Depends(get_session)
) -> dict:
    dispute = dispute_service.create_dispute(
        session,
        order_no=payload.order_no,
        item_code=payload.item_code,
        initiator_role=payload.initiator_role,
        reason=payload.reason,
    )
    return dispute_service.serialize_dispute(dispute, include_snapshot=True)


@router.get("/disputes")
def list_disputes(
    status: str | None = Query(default=None, description="OPEN/REVIEWING/RESOLVED/REJECTED"),
    order_no: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    if status is not None and status not in {s.value for s in DisputeStatus}:
        from app.errors import ValidationError

        raise ValidationError(
            "非法的争议状态", details={"status": status, "allowed": [s.value for s in DisputeStatus]}
        )
    items = dispute_service.list_disputes(session, status=status, order_no=order_no)
    return {
        "total": len(items),
        "items": [dispute_service.serialize_dispute(d) for d in items],
    }


@router.get("/disputes/{dispute_id}")
def get_dispute(
    dispute_id: int,
    include_snapshot: bool = Query(default=True),
    session: Session = Depends(get_session),
) -> dict:
    dispute = dispute_service.get_dispute(session, dispute_id)
    return dispute_service.serialize_dispute(dispute, include_snapshot=include_snapshot)


@router.patch("/disputes/{dispute_id}")
def update_dispute(
    dispute_id: int, payload: DisputeUpdateIn, session: Session = Depends(get_session)
) -> dict:
    dispute = dispute_service.get_dispute(session, dispute_id)
    dispute = dispute_service.update_dispute(
        session,
        dispute,
        status=payload.status,
        resolution=payload.resolution,
        reviewed_by=payload.reviewed_by,
    )
    return dispute_service.serialize_dispute(dispute, include_snapshot=True)
