"""DocumentMetadata(pydantic) ↔ Document(ORM) 변환."""

from __future__ import annotations

from typing import Any

from app.db.models import Document
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import DocumentMetadata


def document_row_values(doc: DocumentMetadata, status: IngestionStatus) -> dict[str, Any]:
    """Document ORM 컬럼 값 딕셔너리(denormalized + 전체 JSON)."""
    ident = doc.identification
    cls = doc.classification
    gov = doc.governance
    life = doc.lifecycle
    return {
        "doc_id": ident.doc_id,
        "source_filename": ident.source_filename,
        "file_format": ident.file_format.value,
        "file_hash": ident.file_hash,
        "message_id": ident.message_id,
        "in_reply_to": ident.in_reply_to,
        "thread_root": ident.thread_root,
        "page_count": ident.page_count,
        "status": status.value,
        "doc_type": cls.doc_type.value if cls.doc_type else None,
        "title": cls.title_normalized,
        "owner": gov.author_name or gov.author_id,       # 작성자(레거시 owner 컬럼 재사용)
        "author_node_id": gov.author_node_id,
        "access_groups": list(gov.access_tokens),        # 조직 열람 토큰
        "lifecycle_status": life.status.value if life.status else None,
        "effective_date": life.effective_date,
        "expiry_date": life.expiry_date,
        "superseded_by": life.superseded_by,
        "metadata_json": doc.model_dump(mode="json"),
    }


def row_to_document(row: Document) -> DocumentMetadata:
    return DocumentMetadata.model_validate(row.metadata_json)
