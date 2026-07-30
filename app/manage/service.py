"""문서 관리 서비스 (색인된 문서의 조회·수정·버전연결·상태변경·삭제).

거버넌스 우선 원칙상 삭제는 기본 '보관(archived)'이고, 하드 삭제는 명시적으로만.
메타데이터·상태가 바뀌면 **Qdrant payload도 동기화**해 검색 하드필터가 항상 맞도록 한다.

낮은 유사도의 개정판은 자동 탐지되지 않으므로, 여기서 사람이 supersede(새 버전 연결)로
기존 문서를 대체할 수 있다. → 대체된 구버전은 검색에서 자동 제외.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from sqlalchemy.orm import Session

from app.db.repositories import DocumentRepository
from app.schemas.enums import DocStatus
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import (
    ClassificationBlock,
    DocumentMetadata,
    GovernanceBlock,
    LifecycleBlock,
    doc_level_payload,
)


class PayloadIndexer(Protocol):
    def set_doc_payload(self, parent_doc_id: str, fields: dict[str, Any]) -> None: ...
    def delete_doc(self, parent_doc_id: str) -> None: ...


class DocumentManager:
    def __init__(self, session: Session, indexer: Optional[PayloadIndexer] = None) -> None:
        self.session = session
        self.repo = DocumentRepository(session)
        self.indexer = indexer

    # ── 조회 ─────────────────────────────────────────────────────────────────
    def list_documents(
        self, text: Optional[str] = None, lifecycle_status: Optional[str] = None,
        doc_type: Optional[str] = None, limit: Optional[int] = None, offset: int = 0,
        author_node_ids=None, indexed_only: bool = False, visible_to=None,
    ) -> list[dict[str, Any]]:
        return self.repo.list_documents(
            text=text, lifecycle_status=lifecycle_status, doc_type=doc_type,
            limit=limit, offset=offset, author_node_ids=author_node_ids,
            indexed_only=indexed_only, visible_to=visible_to)

    def count_documents(
        self, text: Optional[str] = None, lifecycle_status: Optional[str] = None,
        doc_type: Optional[str] = None, author_node_ids=None, indexed_only: bool = False,
        visible_to=None,
    ) -> int:
        return self.repo.count_documents(
            text=text, lifecycle_status=lifecycle_status, doc_type=doc_type,
            author_node_ids=author_node_ids, indexed_only=indexed_only,
            visible_to=visible_to)

    def get(self, doc_id: str) -> Optional[DocumentMetadata]:
        return self.repo.get(doc_id)

    # ── 내부: 저장 + (색인된 경우) Qdrant payload 동기화 ─────────────────────
    def _save(self, doc: DocumentMetadata) -> None:
        doc_id = doc.identification.doc_id
        status = IngestionStatus(self.repo.get_status(doc_id) or IngestionStatus.INDEXED.value)
        self.repo.upsert_document(doc, status)
        if status == IngestionStatus.INDEXED and self.indexer is not None:
            self.indexer.set_doc_payload(doc_id, doc_level_payload(doc))

    # ── 메타데이터 수정 ──────────────────────────────────────────────────────
    def update_metadata(
        self, doc_id: str,
        governance: Optional[GovernanceBlock] = None,
        classification_overrides: Optional[dict] = None,
        lifecycle_overrides: Optional[dict] = None,
    ) -> DocumentMetadata:
        doc = self.repo.get(doc_id)
        if doc is None:
            raise ValueError(f"문서 없음: {doc_id}")
        if governance is not None:
            from app.review.service import _expand_access_tokens
            if governance.author_node_id is None:
                governance = governance.model_copy(update={
                    "author_node_id": doc.governance.author_node_id})
            doc.governance = _expand_access_tokens(self.session, governance)
        if classification_overrides:
            data = doc.classification.model_dump()
            data.update(classification_overrides)
            doc.classification = ClassificationBlock.model_validate(data)
        if lifecycle_overrides:
            data = doc.lifecycle.model_dump()
            data.update(lifecycle_overrides)
            doc.lifecycle = LifecycleBlock.model_validate(data)
        self._save(doc)
        self.session.commit()
        return doc

    # ── 버전 연결(수동 supersede) ────────────────────────────────────────────
    def supersede(self, old_id: str, new_id: str) -> None:
        """old_id 를 new_id 의 이전 버전으로 만든다 → old는 검색에서 제외됨."""
        old = self.repo.get(old_id)
        new = self.repo.get(new_id)
        if old is None or new is None:
            raise ValueError("문서를 찾을 수 없음(old/new)")
        old.lifecycle.status = DocStatus.SUPERSEDED
        old.lifecycle.superseded_by = new_id
        new.lifecycle.supersedes = old_id
        self._save(old)
        self._save(new)
        self.session.commit()

    # ── 상태 변경 / 보관 ─────────────────────────────────────────────────────
    def set_status(self, doc_id: str, status: DocStatus) -> None:
        doc = self.repo.get(doc_id)
        if doc is None:
            raise ValueError(f"문서 없음: {doc_id}")
        doc.lifecycle.status = status
        self._save(doc)
        self.session.commit()

    def archive(self, doc_id: str) -> None:
        self.set_status(doc_id, DocStatus.ARCHIVED)

    # ── 삭제 ─────────────────────────────────────────────────────────────────
    def delete(self, doc_id: str, hard: bool = False) -> None:
        if not hard:
            self.archive(doc_id)   # 기본: 보관(검색 제외, 기록 유지)
            return
        if self.indexer is not None:
            self.indexer.delete_doc(doc_id)
        try:                       # 표 데이터(DuckDB) 정리 — 있으면
            from app.datasets.loader import drop_datasets
            drop_datasets(self.session, doc_id)
        except Exception:
            pass
        self.repo.delete(doc_id)
        self.session.commit()
