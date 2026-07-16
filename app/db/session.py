"""DB 엔진/세션 팩토리.

기본은 설정의 Postgres DSN을 쓰고, 테스트는 SQLite in-memory URL을 주입한다.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db.models import Base


def make_engine(url: str | None = None, echo: bool = False) -> Engine:
    # SQLAlchemy는 postgresql+psycopg 드라이버를 권장(psycopg3).
    dsn = url or settings.postgres_dsn.replace(
        "postgresql://", "postgresql+psycopg://")
    return create_engine(dsn, echo=echo, future=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine: Engine) -> None:
    Base.metadata.create_all(engine)
