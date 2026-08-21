from __future__ import annotations

from sqlmodel import Session, SQLModel, create_engine

from .config import settings

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, echo=False, connect_args=connect_args)


def init_db() -> None:
    from . import models  # noqa: F401  (테이블 등록)
    SQLModel.metadata.create_all(engine)
    if settings.database_url.startswith("sqlite"):
        # 동시 접속(POS PC + 관리자 폰) 대비 — 읽기/쓰기 잠금 완화
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")


def get_session():
    with Session(engine) as session:
        yield session
