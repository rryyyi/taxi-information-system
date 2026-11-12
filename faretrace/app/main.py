"""FastAPI 应用装配：路由、统一错误结构、启动建表与种子数据。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import get_config, seed_on_start
from app.db import SessionLocal, init_db, reset_db
from app.errors import AppError
from app.models import Order
from pathlib import Path

from app.routers import bills, disputes, meta, orders, pages
from app.services.seed import seed_orders


def _seed_if_empty() -> None:
    with SessionLocal() as session:
        count = session.scalar(select(func.count(Order.id)))
        if count:
            return
        seed_orders(session)
        session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if seed_on_start():
        _seed_if_empty()
    yield


def create_app() -> FastAPI:
    cfg = get_config()
    app = FastAPI(
        title="FareTrace 网约车透明账单归因系统",
        version=__version__,
        description=(
            "订单完成后的费用差额归因层：输入预估价快照、实付账单、实际轨迹与参考路径，"
            "输出可解释、可审计、双向透明的账单。全部数据为合成演示数据。"
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "请求参数校验失败",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": f"HTTP_{exc.status_code}",
                    "message": str(exc.detail),
                    "details": {},
                }
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "服务器内部错误",
                    "details": {"type": type(exc).__name__},
                }
            },
        )

    app.include_router(meta.router)
    app.include_router(orders.router)
    app.include_router(bills.router)
    app.include_router(disputes.router)
    app.include_router(pages.router)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
        name="static",
    )

    @app.post("/api/demo/reset", tags=["meta"])
    def demo_reset() -> dict:
        """重置演示库并重新灌入 6 笔种子订单（课程演示用）。"""
        reset_db()
        with SessionLocal() as session:
            seeded = seed_orders(session)
            session.commit()
            return {
                "status": "reset",
                "seeded_orders": [o.order_no for o in seeded],
                "config_version": cfg.config_version,
            }

    return app


app = create_app()
