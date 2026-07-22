"""영속 계층 리포지토리."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session

from app.db.mapping import document_row_values, row_to_document
from app.db.models import AuditLog, Chunk, Document, Feedback, OrgNode, User
from app.org.tree import OrgNodeView, OrgTree
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

    @staticmethod
    def _doc_filters(text: Optional[str], lifecycle_status: Optional[str],
                     doc_type: Optional[str]) -> list:
        conds: list = []
        if text:
            like = f"%{text}%"
            conds.append(
                (Document.source_filename.ilike(like)) | (Document.title.ilike(like)))
        if lifecycle_status:
            conds.append(Document.lifecycle_status == lifecycle_status)
        if doc_type:
            conds.append(Document.doc_type == doc_type)
        return conds

    def list_documents(
        self, text: Optional[str] = None, lifecycle_status: Optional[str] = None,
        doc_type: Optional[str] = None, limit: Optional[int] = None, offset: int = 0,
    ) -> list[dict[str, Any]]:
        """문서 목록(관리용 요약). 필터(파일명·제목/상태/유형) + 페이징.

        limit=None 이면 전체 반환(소규모·테스트용). 대량(수만 건)에서는 limit을 지정해
        화면이 한 번에 모든 행을 로드하지 않도록 한다.
        """
        stmt = select(Document)
        conds = self._doc_filters(text, lifecycle_status, doc_type)
        if conds:
            stmt = stmt.where(and_(*conds))
        stmt = stmt.order_by(Document.updated_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit).offset(offset)
        out = []
        for r in self.session.execute(stmt).scalars():
            out.append({
                "doc_id": r.doc_id, "filename": r.source_filename,
                "doc_type": r.doc_type, "title": r.title,
                "status": r.status, "lifecycle_status": r.lifecycle_status,
                "sensitivity_level": r.sensitivity_level,
                "access_groups": r.access_groups, "owner": r.owner,
                "expiry_date": r.expiry_date.isoformat() if r.expiry_date else None,
                "superseded_by": r.superseded_by,
            })
        return out

    def count_documents(
        self, text: Optional[str] = None, lifecycle_status: Optional[str] = None,
        doc_type: Optional[str] = None,
    ) -> int:
        stmt = select(func.count()).select_from(Document)
        conds = self._doc_filters(text, lifecycle_status, doc_type)
        if conds:
            stmt = stmt.where(and_(*conds))
        return self.session.scalar(stmt) or 0

    # ── 생애주기(만료) 조회 ──────────────────────────────────────────────────
    def doc_ids_to_expire(self, today: date) -> list[str]:
        """만료일이 지났는데도 아직 active 인 문서 id 목록(자동 만료 대상)."""
        return list(self.session.execute(
            select(Document.doc_id).where(
                Document.lifecycle_status == "active",
                Document.expiry_date.is_not(None),
                Document.expiry_date < today)
        ).scalars())

    def count_expiring_soon(self, today: date, until: date) -> int:
        """[today, until] 사이에 만료 예정인 active 문서 수(임박 알림용)."""
        return self.session.scalar(
            select(func.count()).select_from(Document).where(
                Document.lifecycle_status == "active",
                Document.expiry_date.is_not(None),
                Document.expiry_date >= today,
                Document.expiry_date <= until)) or 0

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

    def set_original_path(self, doc_id: str, path: str) -> None:
        row = self.session.get(Document, doc_id)
        if row is not None:
            row.original_path = path
            self.session.flush()

    def get_original_path(self, doc_id: str) -> Optional[str]:
        row = self.session.get(Document, doc_id)
        return row.original_path if row else None


class UserRepository:
    """사용자 ↔ 조직도 배정 + 접근 컨텍스트. 접근은 조직 노드/역할로만 판정한다."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def list_users(self) -> list[dict[str, Any]]:
        out = []
        for u in self.session.execute(select(User).order_by(User.user_id)).scalars():
            out.append({"user_id": u.user_id, "display_name": u.display_name,
                        "org_node_id": u.org_node_id, "org_role": u.org_role})
        return out

    def get_user(self, user_id: str) -> Optional[User]:
        return self.session.get(User, user_id)

    def set_org(self, user_id: str, node_id: Optional[int], role: Optional[str],
                display_name: Optional[str] = None) -> User:
        """사용자를 조직 노드·역할에 배정(없으면 생성). 접근제어의 근거."""
        row = self.session.get(User, user_id)
        if row is None:
            row = User(user_id=user_id, display_name=display_name)
            self.session.add(row)
        if display_name is not None:
            row.display_name = display_name
        row.org_node_id = node_id
        row.org_role = role
        self.session.flush()
        return row

    def get_user_context(self, user_id: str) -> Optional[UserContext]:
        """조직 노드/역할 → 접근 토큰(UserContext.groups)."""
        from app.org.tree import user_tokens
        user = self.session.get(User, user_id)
        if user is None:
            return None
        return UserContext(
            user_id=user_id,
            groups=frozenset(user_tokens(user.org_node_id, user.org_role)),
        )


class OrgRepository:
    """조직도(OrgNode) CRUD + 트리 로드. 관리자만 사용(뷰에서 admin_required)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_node(self, name: str, node_type: str, parent_id: Optional[int] = None,
                    sort_order: int = 0) -> OrgNode:
        node = OrgNode(name=name, node_type=node_type, parent_id=parent_id,
                       sort_order=sort_order)
        self.session.add(node)
        self.session.flush()
        return node

    def rename_node(self, node_id: int, name: str) -> None:
        node = self.session.get(OrgNode, node_id)
        if node is not None:
            node.name = name
            self.session.flush()

    def delete_node(self, node_id: int) -> None:
        """노드 + 하위 전체 삭제. 배정된 사용자는 소속 해제(포터블하게 ORM에서 처리).

        SQLite는 기본적으로 FK cascade 를 강제하지 않으므로 파이썬에서 subtree 를
        직접 지운다(Postgres/SQLite 동일 동작 보장).
        """
        ids = self.load_tree().subtree(node_id)
        if not ids:
            return
        self.session.execute(
            update(User).where(User.org_node_id.in_(ids))
            .values(org_node_id=None, org_role=None))
        for nid in reversed(ids):        # 하위(자식)부터 삭제
            node = self.session.get(OrgNode, nid)
            if node is not None:
                self.session.delete(node)
        self.session.flush()

    def get(self, node_id: int) -> Optional[OrgNode]:
        return self.session.get(OrgNode, node_id)

    def list_nodes(self) -> list[OrgNode]:
        return list(self.session.execute(
            select(OrgNode).order_by(OrgNode.sort_order, OrgNode.id)).scalars())

    def load_tree(self) -> OrgTree:
        views = [OrgNodeView(id=n.id, name=n.name, node_type=n.node_type,
                             parent_id=n.parent_id) for n in self.list_nodes()]
        return OrgTree(views)

    def members(self, node_id: int) -> list[dict[str, Any]]:
        """이 노드에 직접 배정된 사용자 목록."""
        out = []
        for u in self.session.execute(
            select(User).where(User.org_node_id == node_id).order_by(User.user_id)
        ).scalars():
            out.append({"user_id": u.user_id, "display_name": u.display_name,
                        "org_role": u.org_role})
        return out


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


class ChatRepository:
    """채팅 대화·메시지 저장/조회 (ChatGPT 스타일 멀티턴)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create_conversation(self, user_id: str, title: str | None = None) -> int:
        from app.db.models import Conversation
        conv = Conversation(user_id=user_id, title=title)
        self.session.add(conv)
        self.session.flush()
        return conv.id

    def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        from app.db.models import Conversation
        rows = self.session.execute(
            select(Conversation).where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
        ).scalars()
        return [{"id": c.id, "title": c.title or "(새 대화)",
                 "updated_at": c.updated_at.isoformat() if c.updated_at else None}
                for c in rows]

    def add_message(self, conversation_id: int, role: str, text: str,
                    use_rag: bool = False, sources: list | None = None) -> int:
        from app.db.models import ChatMessage, Conversation
        msg = ChatMessage(conversation_id=conversation_id, role=role, text=text,
                          use_rag=use_rag, sources_json=sources or [])
        self.session.add(msg)
        # 제목이 없으면 첫 사용자 질문으로 자동 설정 + updated_at 갱신
        conv = self.session.get(Conversation, conversation_id)
        if conv is not None:
            if conv.title is None and role == "user":
                conv.title = text[:60]
            from datetime import datetime, timezone
            conv.updated_at = datetime.now(timezone.utc)
        self.session.flush()
        return msg.id

    def get_messages(self, conversation_id: int, user_id: str | None = None,
                     limit: int | None = None) -> list[dict[str, Any]]:
        """대화 메시지(오래된 순). user_id 지정 시 소유자 검증(남의 대화 차단)."""
        from app.db.models import ChatMessage, Conversation
        conv = self.session.get(Conversation, conversation_id)
        if conv is None or (user_id is not None and conv.user_id != user_id):
            return []
        stmt = (select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.id))
        rows = list(self.session.execute(stmt).scalars())
        if limit is not None:
            rows = rows[-limit:]
        return [{"id": m.id, "role": m.role, "text": m.text,
                 "use_rag": m.use_rag, "sources": m.sources_json or []}
                for m in rows]
