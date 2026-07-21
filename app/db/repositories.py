"""영속 계층 리포지토리."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.mapping import document_row_values, row_to_document
from app.db.models import AuditLog, Chunk, Document, Feedback, Group, User, UserGroup
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

    def get_status(self, doc_id: str) -> Optional[str]:
        return self.session.execute(
            select(Document.status).where(Document.doc_id == doc_id)
        ).scalar_one_or_none()

    def list_documents(self, text: Optional[str] = None) -> list[dict[str, Any]]:
        """문서 목록(관리용 요약). text가 주어지면 파일명/제목 부분일치 필터."""
        stmt = select(Document).order_by(Document.updated_at.desc())
        if text:
            like = f"%{text}%"
            stmt = stmt.where(
                (Document.source_filename.ilike(like)) | (Document.title.ilike(like)))
        out = []
        for r in self.session.execute(stmt).scalars():
            out.append({
                "doc_id": r.doc_id, "filename": r.source_filename,
                "doc_type": r.doc_type, "title": r.title,
                "status": r.status, "lifecycle_status": r.lifecycle_status,
                "sensitivity_level": r.sensitivity_level,
                "access_groups": r.access_groups, "owner": r.owner,
                "superseded_by": r.superseded_by,
            })
        return out

    def delete(self, doc_id: str) -> None:
        row = self.session.get(Document, doc_id)
        if row is not None:
            self.session.delete(row)   # chunks는 cascade 삭제
            self.session.flush()

    def set_similar_candidates(self, doc_id: str, candidates: list) -> None:
        row = self.session.get(Document, doc_id)
        if row is not None:
            row.similar_candidates = candidates
            self.session.flush()

    def get_similar_candidates(self, doc_id: str) -> list:
        row = self.session.get(Document, doc_id)
        return list(row.similar_candidates) if row and row.similar_candidates else []


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

    def list_users(self) -> list[dict[str, Any]]:
        out = []
        for u in self.session.execute(select(User).order_by(User.user_id)).scalars():
            groups = list(self.session.execute(
                select(UserGroup.group_name).where(UserGroup.user_id == u.user_id)
            ).scalars())
            out.append({"user_id": u.user_id, "display_name": u.display_name,
                        "position": u.position, "job": u.job,
                        "clearance": u.clearance, "groups": groups})
        return out

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


class FeedbackRepository:
    """답변 피드백 저장·조회 (틀린 답변 교정 워크플로우의 시작점)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, query_text: str, rating: str, user_id: str | None = None,
               answer_text: str | None = None, note: str | None = None,
               cited_doc_ids: list[str] | None = None) -> int:
        fb = Feedback(
            user_id=user_id, query_text=query_text, answer_text=answer_text,
            rating=rating, note=note, cited_doc_ids=cited_doc_ids or [])
        self.session.add(fb)
        self.session.flush()
        return fb.id

    def list_unresolved(self) -> list[Feedback]:
        return list(self.session.execute(
            select(Feedback).where(Feedback.rating == "down",
                                   Feedback.resolved.is_(False))
            .order_by(Feedback.ts)
        ).scalars())

    def mark_resolved(self, feedback_id: int) -> None:
        fb = self.session.get(Feedback, feedback_id)
        if fb is not None:
            fb.resolved = True
            self.session.flush()
