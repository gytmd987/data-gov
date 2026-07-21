"""SQLAlchemy ORM 모델 (영속 계층).

포터블하게 sqlalchemy.JSON/Date 를 사용해 Postgres(운영) / SQLite(테스트) 모두 지원한다.
벡터는 Qdrant에 있고, 여기서는 문서·청크·사용자·그룹·감사로그를 영속화한다.

denormalized 컬럼(status, file_hash, sensitivity_level 등)은 조회·중복탐지·필터용이고,
전체 메타데이터 원본은 documents.metadata_json 에 보존한다.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_filename: Mapped[str] = mapped_column(String(512))
    file_format: Mapped[str] = mapped_column(String(16))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 적재 상태 머신
    status: Mapped[str] = mapped_column(String(32), index=True)

    # 분류(조회용 denormalized)
    doc_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # 거버넌스(조회/필터용 denormalized)
    sensitivity_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    contains_pii: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    access_groups: Mapped[list] = mapped_column(JSON, default=list)

    # 생애주기
    lifecycle_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 전체 메타데이터 원본
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("file_hash", name="uq_documents_file_hash"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    parent_doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.doc_id", ondelete="CASCADE"), index=True)
    chunk_type: Mapped[str] = mapped_column(String(16))
    section_title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    indexed: Mapped[bool] = mapped_column(Boolean, default=False)

    document: Mapped[Document] = relationship(back_populates="chunks")


class Group(Base):
    __tablename__ = "groups"

    group_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    clearance: Mapped[str] = mapped_column(String(16))   # SensitivityLevel value
    position: Mapped[str | None] = mapped_column(String(64), nullable=True)   # 직책
    job: Mapped[str | None] = mapped_column(String(64), nullable=True)        # 직무


class UserGroup(Base):
    __tablename__ = "user_groups"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    group_name: Mapped[str] = mapped_column(
        ForeignKey("groups.group_name", ondelete="CASCADE"), primary_key=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    event: Mapped[dict] = mapped_column(JSON, default=dict)   # 상세(필터/검색·인용 id 등)
