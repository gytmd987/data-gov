"""영속 계층 리포지토리."""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Optional

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.db.mapping import document_row_values, row_to_document
from app.db.models import (
    AuditLog,
    Chunk,
    Document,
    DocumentAccessToken,
    DocumentRelation,
    DocumentRequest,
    Feedback,
    OrgNode,
    User,
    UserOrgNode,
)
from app.org.tree import OrgNodeView, OrgTree
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import DocumentMetadata
from app.search.access import UserContext, Visibility


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def hash_lookup(self, file_hash: str) -> Optional[str]:
        """중복 탐지: file_hash로 기존 doc_id 반환(intake에 주입).

        '등록 완료(INDEXED)' 문서만 진짜 중복으로 본다. 예전에 올리다 만
        (검토대기/차단) 문서가 같은 해시로 남아 있으면 재업로드를 막을 뿐 목록엔
        안 보이므로, 그 stale 행은 제거하고 None 을 반환해 재업로드를 허용한다.
        """
        row = self.session.execute(
            select(Document.doc_id, Document.status).where(
                Document.file_hash == file_hash)).first()
        if row is None:
            return None
        doc_id, status = row
        if status == IngestionStatus.INDEXED.value:
            return doc_id
        self.delete(doc_id)   # 색인 전 stale 문서 폐기(청크 cascade) → 재업로드 진행
        return None

    def upsert_document(self, doc: DocumentMetadata, status: IngestionStatus) -> None:
        values = document_row_values(doc, status)
        row = self.session.get(Document, doc.identification.doc_id)
        if row is None:
            self.session.add(Document(**values))
        else:
            for k, v in values.items():
                setattr(row, k, v)
        self.session.flush()
        self.sync_access_tokens(doc.identification.doc_id, values["access_groups"])

    def sync_access_tokens(self, doc_id: str, tokens: Iterable[str]) -> None:
        """documents.access_groups → document_access_tokens 행 동기화.

        열람 권한이 바뀌면 반드시 여기를 통과해야 목록·검색 필터가 최신이 된다.
        """
        wanted = {str(t) for t in (tokens or []) if str(t).strip()} or {"*"}
        current = set(self.session.execute(
            select(DocumentAccessToken.token)
            .where(DocumentAccessToken.doc_id == doc_id)).scalars())
        for gone in current - wanted:
            self.session.execute(delete(DocumentAccessToken).where(
                DocumentAccessToken.doc_id == doc_id,
                DocumentAccessToken.token == gone))
        for added in wanted - current:
            self.session.add(DocumentAccessToken(doc_id=doc_id, token=added))
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
    def _visibility_cond(visible_to: Optional["Visibility"]):
        """'열람 권한이 있거나(토큰) 관리 대상인(작성부서)' 문서 조건. None 이면 무제한.

        읽기와 관리는 별개다. 부서장은 자기 부서 문서를 (열람 토큰이 없어도) 관리해야
        하고, 반대로 상위 부서 공개 문서는 내 부서가 아니어도 읽을 수 있어야 한다.
        """
        if visible_to is None or visible_to.unrestricted:
            return None
        clauses = []
        tokens = set(visible_to.read_tokens or set()) | {"*"}
        clauses.append(Document.doc_id.in_(
            select(DocumentAccessToken.doc_id).where(
                DocumentAccessToken.token.in_(sorted(tokens)))))
        if visible_to.manage_node_ids:
            clauses.append(Document.author_node_id.in_(
                sorted(visible_to.manage_node_ids)))
        return or_(*clauses)

    @classmethod
    def _doc_filters(cls, text: Optional[str], lifecycle_status: Optional[str],
                     doc_type: Optional[str],
                     author_node_ids: Optional[Iterable[int]] = None,
                     indexed_only: bool = False,
                     visible_to: Optional["Visibility"] = None) -> list:
        conds: list = []
        if text:
            like = f"%{text}%"
            conds.append(
                (Document.source_filename.ilike(like)) | (Document.title.ilike(like)))
        if lifecycle_status:
            conds.append(Document.lifecycle_status == lifecycle_status)
        if doc_type:
            conds.append(Document.doc_type == doc_type)
        if author_node_ids is not None:
            conds.append(Document.author_node_id.in_(list(author_node_ids)))
        if indexed_only:                 # 검토 대기·차단 문서는 목록에서 제외
            conds.append(Document.status == IngestionStatus.INDEXED.value)
        vis = cls._visibility_cond(visible_to)
        if vis is not None:
            conds.append(vis)
        return conds

    # 목록 정렬 기준 — 화면 드롭다운 값과 1:1로 맞춘다.
    SORT_FIELDS = {
        "updated": Document.updated_at,        # 최근 변경
        "created": Document.created_at,        # 등록일
        "effective": Document.effective_date,  # 작성일
        "title": Document.title,
        "doc_type": Document.doc_type,
        "status": Document.lifecycle_status,
        "department": Document.author_node_id,
    }

    @classmethod
    def _order_by(cls, sort: str, desc: bool) -> list:
        col = cls.SORT_FIELDS.get(sort, Document.updated_at)
        primary = col.desc() if desc else col.asc()
        # 값이 같거나 비었을 때 순서가 흔들리지 않도록 보조 정렬을 붙인다.
        return [primary, Document.updated_at.desc()]

    def readable_doc_ids(self, doc_ids: Iterable[str],
                         visible_to: Optional["Visibility"]) -> set[str]:
        """주어진 문서들 중 이 사용자가 볼 수 있는 것만 추린다(연관 문서 표시 등)."""
        ids = [d for d in dict.fromkeys(doc_ids) if d]
        if not ids:
            return set()
        if visible_to is None or visible_to.unrestricted:
            return set(ids)
        stmt = select(Document.doc_id).where(Document.doc_id.in_(ids))
        vis = self._visibility_cond(visible_to)
        if vis is not None:
            stmt = stmt.where(vis)
        return set(self.session.execute(stmt).scalars())

    def last_upload_defaults(self, author_id: str) -> Optional[dict[str, Any]]:
        """이 사람이 **확정(등록 완료)** 한 가장 최근 문서의 작성부서·접근권한.

        다음 업로드/검토의 기본값으로 쓴다. 검토 대기(미확정) 문서는 제외하므로,
        여러 파일을 연속 검토할 때 '직전에 확정한 문서'의 설정이 그대로 이어진다.
        """
        if not author_id:
            return None
        rows = self.session.execute(
            select(Document).where(Document.status == IngestionStatus.INDEXED.value)
            .order_by(Document.updated_at.desc()).limit(100)).scalars()
        for row in rows:
            meta = row.metadata_json or {}
            gov = meta.get("governance", {}) if isinstance(meta, dict) else {}
            if gov.get("author_id") != author_id:
                continue
            return {"author_node_id": row.author_node_id,
                    "access_selections": list(gov.get("access_selections") or [])}
        return None

    def find_active_by_title_format(self, title: Optional[str], file_format: str,
                                    exclude_doc_id: Optional[str] = None
                                    ) -> Optional[dict[str, Any]]:
        """제목+형식이 같은 비-아카이브 문서(중복 후보). 없으면 None."""
        if not title:
            return None
        stmt = select(Document).where(
            Document.title == title, Document.file_format == file_format,
            Document.lifecycle_status != "archived")
        if exclude_doc_id:
            stmt = stmt.where(Document.doc_id != exclude_doc_id)
        row = self.session.execute(stmt).scalars().first()
        if row is None:
            return None
        return {"doc_id": row.doc_id, "filename": row.source_filename,
                "author_node_id": row.author_node_id, "owner": row.owner}

    def list_documents(
        self, text: Optional[str] = None, lifecycle_status: Optional[str] = None,
        doc_type: Optional[str] = None, limit: Optional[int] = None, offset: int = 0,
        author_node_ids: Optional[Iterable[int]] = None, indexed_only: bool = False,
        visible_to: Optional[Visibility] = None,
        sort: str = "updated", desc: bool = True,
    ) -> list[dict[str, Any]]:
        """문서 목록(관리용 요약). 필터(파일명·제목/상태/유형) + 페이징.

        limit=None 이면 전체 반환(소규모·테스트용). 대량(수만 건)에서는 limit을 지정해
        화면이 한 번에 모든 행을 로드하지 않도록 한다.

        visible_to 를 주면 권한 밖 문서는 **쿼리 단에서** 빠진다(건수·페이징도 일치).
        """
        stmt = select(Document)
        conds = self._doc_filters(text, lifecycle_status, doc_type, author_node_ids,
                                  indexed_only, visible_to)
        if conds:
            stmt = stmt.where(and_(*conds))
        stmt = stmt.order_by(*self._order_by(sort, desc))
        if limit is not None:
            stmt = stmt.limit(limit).offset(offset)
        out = []
        for r in self.session.execute(stmt).scalars():
            out.append({
                "doc_id": r.doc_id, "filename": r.source_filename,
                "doc_type": r.doc_type, "title": r.title,
                "status": r.status, "lifecycle_status": r.lifecycle_status,
                "access_groups": r.access_groups, "owner": r.owner,
                "author_node_id": r.author_node_id,
                "effective_date": r.effective_date.isoformat() if r.effective_date else None,
                "expiry_date": r.expiry_date.isoformat() if r.expiry_date else None,
                "superseded_by": r.superseded_by,
            })
        return out

    def count_documents(
        self, text: Optional[str] = None, lifecycle_status: Optional[str] = None,
        doc_type: Optional[str] = None,
        author_node_ids: Optional[Iterable[int]] = None, indexed_only: bool = False,
        visible_to: Optional[Visibility] = None,
    ) -> int:
        stmt = select(func.count()).select_from(Document)
        conds = self._doc_filters(text, lifecycle_status, doc_type, author_node_ids,
                                  indexed_only, visible_to)
        if conds:
            stmt = stmt.where(and_(*conds))
        return self.session.scalar(stmt) or 0

    # ── 생애주기(만료) 조회 ──────────────────────────────────────────────────
    _SEARCHABLE = ("active", "archived")   # 검색 노출 상태(유효·보관)

    def doc_ids_to_expire(self, today: date) -> list[str]:
        """만료일이 지났는데도 아직 유효/보관 인 문서 id 목록(자동 만료 대상)."""
        return list(self.session.execute(
            select(Document.doc_id).where(
                Document.lifecycle_status.in_(self._SEARCHABLE),
                Document.expiry_date.is_not(None),
                Document.expiry_date < today)
        ).scalars())

    def count_expiring_soon(self, today: date, until: date) -> int:
        """[today, until] 사이에 만료 예정인 유효/보관 문서 수(임박 알림용)."""
        return self.session.scalar(
            select(func.count()).select_from(Document).where(
                Document.lifecycle_status.in_(self._SEARCHABLE),
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
    """사용자 ↔ 조직도 소속(다대다) + 접근 컨텍스트.

    소속은 여러 노드가 가능하고, 부서장(리더) 지정은 OrgNode.leader_id 로 한다.
    접근 토큰: 소속 노드마다 n:{node}, 리더인 노드마다 n:{node}+h:{node}.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def _member_nodes(self, user_id: str) -> list[int]:
        return list(self.session.execute(
            select(UserOrgNode.node_id).where(UserOrgNode.user_id == user_id)).scalars())

    def _led_nodes(self, user_id: str) -> list[int]:
        return list(self.session.execute(
            select(OrgNode.id).where(OrgNode.leader_id == user_id)).scalars())

    def list_users(self) -> list[dict[str, Any]]:
        out = []
        for u in self.session.execute(select(User).order_by(User.user_id)).scalars():
            out.append({"user_id": u.user_id, "display_name": u.display_name,
                        "node_ids": self._member_nodes(u.user_id),
                        "led_ids": self._led_nodes(u.user_id)})
        return out

    def get_user(self, user_id: str) -> Optional[User]:
        return self.session.get(User, user_id)

    def member_nodes(self, user_id: str) -> list[int]:
        """이 사용자가 소속된 노드 id 목록."""
        return self._member_nodes(user_id)

    def primary_node(self, user_id: str) -> Optional[int]:
        """작성부서 기본값 등에 쓸 대표 소속 노드(첫 소속). 없으면 None."""
        nodes = self._member_nodes(user_id)
        return nodes[0] if nodes else None

    def delete_user(self, user_id: str) -> bool:
        row = self.session.get(User, user_id)
        if row is None:
            return False
        self.session.execute(delete(UserOrgNode).where(UserOrgNode.user_id == user_id))
        self.session.execute(
            update(OrgNode).where(OrgNode.leader_id == user_id).values(leader_id=None))
        self.session.delete(row)
        self.session.flush()
        return True

    def set_memberships(self, user_id: str, node_ids: Iterable[int],
                        display_name: Optional[str] = None) -> User:
        """사용자의 소속 노드 집합을 통째로 교체(없으면 생성)."""
        row = self.session.get(User, user_id)
        if row is None:
            row = User(user_id=user_id, display_name=display_name)
            self.session.add(row)
        if display_name is not None:
            row.display_name = display_name
        self.session.execute(delete(UserOrgNode).where(UserOrgNode.user_id == user_id))
        for nid in dict.fromkeys(int(n) for n in node_ids):   # 중복 제거
            self.session.add(UserOrgNode(user_id=user_id, node_id=nid))
        self.session.flush()
        return row

    def set_org(self, user_id: str, node_id: Optional[int], role: Optional[str] = None,
                display_name: Optional[str] = None) -> User:
        """하위호환: 단일 노드 배정(+역할이 부서장이면 그 노드 리더로 지정)."""
        from app.org.tree import is_head_role
        row = self.set_memberships(user_id, [node_id] if node_id else [], display_name)
        if node_id and is_head_role(role):
            node = self.session.get(OrgNode, node_id)
            if node is not None:
                node.leader_id = user_id
                self.session.flush()
        return row

    def get_user_context(self, user_id: str) -> Optional[UserContext]:
        """소속·리더 노드 → 접근 토큰(UserContext.groups)."""
        if self.session.get(User, user_id) is None:
            return None
        tokens: set[str] = set()
        for m in self._member_nodes(user_id):
            tokens.add(f"n:{m}")
        for led in self._led_nodes(user_id):
            tokens.add(f"n:{led}")
            tokens.add(f"h:{led}")
        return UserContext(user_id=user_id, groups=frozenset(tokens))


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
        # 소속(UserOrgNode)·리더 지정 해제 후 노드 삭제(포터블하게 ORM에서 처리)
        self.session.execute(delete(UserOrgNode).where(UserOrgNode.node_id.in_(ids)))
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

    def set_leader(self, node_id: int, user_id: Optional[str]) -> None:
        """이 조직의 리더(부서장) 지정/해제."""
        node = self.session.get(OrgNode, node_id)
        if node is not None:
            node.leader_id = user_id or None
            self.session.flush()

    def leader(self, node_id: int) -> Optional[dict[str, Any]]:
        node = self.session.get(OrgNode, node_id)
        if node is None or not node.leader_id:
            return None
        u = self.session.get(User, node.leader_id)
        return {"user_id": node.leader_id,
                "display_name": u.display_name if u is not None else None}

    def nodes_led_by(self, user_id: str) -> list[int]:
        return list(self.session.execute(
            select(OrgNode.id).where(OrgNode.leader_id == user_id)).scalars())

    def members(self, node_id: int) -> list[dict[str, Any]]:
        """이 노드에 소속된 사용자 목록."""
        out = []
        for u in self.session.execute(
            select(User).join(UserOrgNode, User.user_id == UserOrgNode.user_id)
            .where(UserOrgNode.node_id == node_id).order_by(User.user_id)
        ).scalars():
            out.append({"user_id": u.user_id, "display_name": u.display_name})
        return out


class RequestRepository:
    """문서 수정/삭제 요청 저장·조회·처리."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, doc_id: str, request_type: str, requester_id: str,
               doc_title: Optional[str] = None, author_node_id: Optional[int] = None,
               target_admin_id: Optional[str] = None, payload: Optional[dict] = None,
               note: Optional[str] = None) -> int:
        req = DocumentRequest(
            doc_id=doc_id, doc_title=doc_title, author_node_id=author_node_id,
            request_type=request_type, requester_id=requester_id,
            target_admin_id=target_admin_id, payload_json=payload or {}, note=note)
        self.session.add(req)
        self.session.flush()
        return req.id

    def get(self, req_id: int) -> Optional[DocumentRequest]:
        return self.session.get(DocumentRequest, req_id)

    def list_pending(self) -> list[DocumentRequest]:
        return list(self.session.execute(
            select(DocumentRequest).where(DocumentRequest.status == "pending")
            .order_by(DocumentRequest.created_at.desc())).scalars())

    def resolve(self, req_id: int, status: str, resolved_by: str) -> None:
        from datetime import datetime, timezone
        req = self.session.get(DocumentRequest, req_id)
        if req is not None:
            req.status = status
            req.resolved_by = resolved_by
            req.resolved_at = datetime.now(timezone.utc)
            self.session.flush()

    def pending_delete_doc_ids(self) -> set[str]:
        """삭제 승인 대기 중인 문서 id 집합(같은 제목 재업로드 잠금용)."""
        return set(self.session.execute(
            select(DocumentRequest.doc_id).where(
                DocumentRequest.request_type == "delete",
                DocumentRequest.status == "pending")).scalars())


class RelationRepository:
    """문서 간 '연관' 관계 저장·조회(무방향, 쌍 정규화)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _pair(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a <= b else (b, a)

    def link(self, a: str, b: str, source: str = "auto", confidence: float = 1.0,
             reason: Optional[str] = None, created_by: Optional[str] = None) -> None:
        if a == b:
            return
        da, db = self._pair(a, b)
        exists = self.session.execute(
            select(DocumentRelation).where(
                DocumentRelation.doc_a == da, DocumentRelation.doc_b == db)
        ).scalars().first()
        if exists is not None:
            # 사람이 확정한 관계는 자동 감지가 덮어쓰지 않음
            if source == "human":
                exists.source = "human"
                exists.reason = reason or exists.reason
            return
        self.session.add(DocumentRelation(
            doc_a=da, doc_b=db, source=source, confidence=confidence,
            reason=reason, created_by=created_by))
        self.session.flush()

    def unlink(self, a: str, b: str) -> None:
        da, db = self._pair(a, b)
        row = self.session.execute(
            select(DocumentRelation).where(
                DocumentRelation.doc_a == da, DocumentRelation.doc_b == db)
        ).scalars().first()
        if row is not None:
            self.session.delete(row)
            self.session.flush()

    def related_ids(self, doc_id: str) -> list[dict[str, Any]]:
        """이 문서와 연관된 상대 문서 [{doc_id, source, reason}] 목록."""
        rows = self.session.execute(
            select(DocumentRelation).where(
                (DocumentRelation.doc_a == doc_id) | (DocumentRelation.doc_b == doc_id))
        ).scalars()
        out = []
        for r in rows:
            other = r.doc_b if r.doc_a == doc_id else r.doc_a
            out.append({"doc_id": other, "source": r.source, "reason": r.reason})
        return out


class DatasetRepository:
    """표 데이터 카탈로그(Dataset) — DuckDB 테이블 목록·스키마·권한."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, doc_id: str, sheet: str, table_name: str, columns: list[dict],
               row_count: int, access_groups: list[str]) -> None:
        from app.db.models import Dataset
        row = self.session.execute(
            select(Dataset).where(Dataset.table_name == table_name)).scalars().first()
        if row is None:
            row = Dataset(doc_id=doc_id, sheet=sheet, table_name=table_name)
            self.session.add(row)
        row.doc_id = doc_id
        row.sheet = sheet
        row.columns_json = columns
        row.row_count = row_count
        row.access_groups = list(access_groups)
        self.session.flush()

    def by_doc(self, doc_id: str) -> list:
        from app.db.models import Dataset
        return list(self.session.execute(
            select(Dataset).where(Dataset.doc_id == doc_id)).scalars())

    def drop_for_doc(self, doc_id: str) -> list[str]:
        """카탈로그에서 이 문서의 데이터셋 제거 → 삭제해야 할 DuckDB 테이블명 반환."""
        from app.db.models import Dataset
        rows = self.by_doc(doc_id)
        names = [r.table_name for r in rows]
        for r in rows:
            self.session.delete(r)
        self.session.flush()
        return names

    def list_visible(self, user_ctx) -> list[dict[str, Any]]:
        """사용자가 접근 가능 + 문서가 검색 노출 상태(유효·보관)인 데이터셋 목록."""
        from app.db.models import Dataset
        tokens = set(getattr(user_ctx, "groups", set()) or set())
        rows = self.session.execute(
            select(Dataset, Document.status, Document.lifecycle_status,
                   Document.source_filename, Document.title)
            .join(Document, Dataset.doc_id == Document.doc_id)).all()
        out = []
        for ds, status, life, filename, title in rows:
            if status != IngestionStatus.INDEXED.value:
                continue
            if life not in ("active", "archived"):
                continue
            ag = ds.access_groups or []
            if "*" not in ag and not (set(ag) & tokens):
                continue
            out.append({"doc_id": ds.doc_id, "sheet": ds.sheet,
                        "table_name": ds.table_name, "columns": ds.columns_json or [],
                        "row_count": ds.row_count,
                        "filename": filename, "title": title})
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
