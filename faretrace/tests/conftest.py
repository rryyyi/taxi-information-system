"""测试夹具：临时 SQLite（不污染演示库）+ TestClient。

注意：环境变量必须在导入 app 模块之前设置，因为 app.db 在导入时创建 engine。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="faretrace-test-"))
os.environ["FARETRACE_DB_URL"] = f"sqlite:///{( _TMP_DIR / 'test.db').as_posix()}"
os.environ["FARETRACE_SEED_ON_START"] = "0"
os.environ["FARETRACE_CONFIG"] = str(Path(__file__).resolve().parents[1] / "config" / "attribution.yaml")

from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.config import Config, load_config, get_config  # noqa: E402
from app.db import SessionLocal, init_db, reset_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.seed import seed_orders  # noqa: E402
from app.services import ingest  # noqa: E402
from app.services.attribution import run_attribution  # noqa: E402


@pytest.fixture(scope="session")
def cfg() -> Config:
    return get_config()


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """纯单元测试用内存库。"""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, future=True)
    init_db(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    sess = factory()
    try:
        yield sess
        sess.commit()
    finally:
        sess.close()


@pytest.fixture()
def db_engine() -> Iterator[Engine]:
    """接口/端到端测试用文件库（与 app.db 的 engine 相同位置）。"""
    reset_db()
    yield __import__("app.db", fromlist=["engine"]).engine


@pytest.fixture()
def seeded_session() -> Iterator[Session]:
    """已灌入 6 笔种子订单的会话。"""
    reset_db()
    sess = SessionLocal()
    try:
        seed_orders(sess)
        sess.commit()
        yield sess
    finally:
        sess.close()


@pytest.fixture()
def client() -> Iterator[TestClient]:
    reset_db()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def seeded_client() -> Iterator[TestClient]:
    reset_db()
    with TestClient(app) as test_client:
        with SessionLocal() as session:
            seed_orders(session)
            session.commit()
        yield test_client


@pytest.fixture()
def attribute_order():
    """便捷函数：对已落库的订单执行归因（不持久化）。"""
    def _run(session: Session, order_no: str):
        order = ingest.get_order(session, order_no)
        return run_attribution(session, order, get_config())

    return _run
