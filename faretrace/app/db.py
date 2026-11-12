"""数据库会话与建表工具（SQLite + 同步 SQLAlchemy 2.0）。

课程演示约定：
* 单文件 SQLite，无需单独数据库服务；
* 不使用迁移工具，`reset_db()` 重建库（演示复位）；
* 每个请求一个 Session，异常回滚。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import database_url


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None) -> Engine:
    resolved = url or database_url()
    connect_args = {"check_same_thread": False} if resolved.startswith("sqlite") else {}
    return create_engine(resolved, echo=False, future=True, connect_args=connect_args)


engine: Engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db(target_engine: Engine | None = None) -> None:
    """建表（幂等）。必须先 import 模型模块，确保元数据已注册。"""
    from app import models  # noqa: F401  (注册 ORM 映射)

    Base.metadata.create_all(bind=target_engine or engine)


def reset_db(target_engine: Engine | None = None) -> None:
    """删表重建，用于演示复位与测试隔离。"""
    from app import models  # noqa: F401

    Base.metadata.drop_all(bind=target_engine or engine)
    Base.metadata.create_all(bind=target_engine or engine)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：请求级会话。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope(session_factory: sessionmaker[Session] = SessionLocal) -> Iterator[Session]:
    """脚本/测试用上下文管理器。"""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
