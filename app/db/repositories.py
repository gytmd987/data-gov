"""영속 계층 리포지토리."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.mapping import document_row_values, row_to_document
from app.db.models import AuditLog, Chunk, Document, Group, User, UserGroup
from app.schemas.enums import SensitivityLevel
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import DocumentMetadata
from app.search.access import UserContext


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def hash_lookup(self, file_hash: str) -> Optional[str]:
        """중복 탐지: file_hash로 기존 doc_id 반환(intake에 주입)."""
        return self.session.execute(
            select(Document.doc_id).where(Document.file_hash == file_hash)
        ).scalar_one_or_none()

    def upsert_document(self, doc: DocumentMetadata, status: IngestionStatus) -> None:
        values = document_row_values(doc, status)
        row = self.session.get(Document, doc.identification.doc_id)
        if row is None:
            self.session.add(Document(**values))
        else:
            for k, v in values.items():
                setattr(row, k, v)
        self.session.flush()

    def upsert_chunks(self, parent_doc_id: str, chunks) -> None:
        for c in chunks:
            row = self.session.get(Chunk, c.meta.chunk_id)
            values = dict(
                chunk_id=c.meta.chunk_id,
                parent_doc_id=parent_doc_id,
                chunk_type=c.meta.chunk_type.value,
                section_title=c.meta.section_title,
                page_no=c.meta.page_no,
                text=c.text,
            )
            if row is None:
                self.session.add(Chunk(**values, indexed=False))
            else:
                for k, v in values.items():
                    setattr(row, k, v)
        self.session.flush()

    def set_status(self, doc_id: str, status: IngestionStatus) -> None:
        row = self.session.get(Document, doc_id)
        if row is not None:
            row.status = status.value
            self.session.flush()

    def mark_chunks_indexed(self, doc_id: str) -> None:
        for c in self.session.execute(
            select(Chunk).where(Chunk.parent_doc_id == doc_id)
        ).scalars():
            c.indexed = True
        self.session.flush()

    def get(self, doc_id: str) -> Optional[DocumentMetadata]:
        row = self.session.get(Document, doc_id)
        return row_to_document(row) if row is not None else None

    def list_by_status(self, status: IngestionStatus) -> list[str]:
        return list(self.session.execute(
            select(Document.doc_id).where(Document.status == status.value)
        ).scalars())


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_group(self, group_name: str, description: str | None = None) -> None:
        row = self.session.get(Group, group_name)
        if row is None:
            self.session.add(Group(group_name=group_name, description=description))
        else:
            row.description = description
        self.session.flush()

    def upsert_user(self, user_id: str, clearance: SensitivityLevel,
                    display_name: str | None = None) -> None:
        row = self.session.get(User, user_id)
        if row is None:
            self.session.add(User(user_id=user_id, clearance=clearance.value,
                                  display_name=display_name))
        else:
            row.clearance = clearance.value
            row.display_name = display_name
        self.session.flush()

    def add_user_to_group(self, user_id: str, group_name: str) -> None:
        exists = self.session.get(UserGroup, {"user_id": user_id, "group_name": group_name})
        if exists is None:
            self.session.add(UserGroup(user_id=user_id, group_name=group_name))
            self.session.flush()

    def upsert_user_with_role(
        self, user_id: str, position: str, job: str, display_name: str | None = None,
    ) -> tuple[set[str], str]:
        """직책·직무로부터 config/system.yaml 규칙에 따라 그룹·clearance를 계산해 사용자 생성.

        같은 직책이라도 직무에 따라, 같은 직무라도 직책에 따라 권한이 달라진다.
        반환: (부여된 access_groups, clearance)
        """
        from app import system_config

        groups, clearance = system_config.resolve_access(position, job)
        # 그룹 레코드 보장
        for g in groups:
            self.upsert_group(g)
        row = self.session.get(User, user_id)
        if row is None:
            self.session.add(User(user_id=user_id, clearance=clearance,
                                  display_name=display_name, position=position, job=job))
        else:
            row.clearance = clearance
            row.display_name = display_name
            row.position = position
            row.job = job
        self.session.flush()
        for g in groups:
            self.add_user_to_group(user_id, g)
        return groups, clearance

    def known_access_groups(self) -> list[str]:
        return list(self.session.execute(select(Group.group_name)).scalars())

    def get_user_context(self, user_id: str) -> Optional[UserContext]:
        user = self.session.get(User, user_id)
        if user is None:
            return None
        groups = self.session.execute(
            select(UserGroup.group_name).where(UserGroup.user_id == user_id)
        ).scalars()
        return UserContext(
            user_id=user_id,
            groups=frozenset(groups),
            clearance=SensitivityLevel(user.clearance),
        )


class AuditRepository:
    """search.pipeline.AuditSink 프로토콜 구현(append-only)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, event: dict[str, Any]) -> None:
        self.session.add(AuditLog(
            user_id=event.get("user_id"),
            action=event.get("action", "unknown"),
            query_text=event.get("query_text"),
            event=event,
        ))
        self.session.flush()
