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
        "page_count": ident.page_count,
        "status": status.value,
        "doc_type": cls.doc_type.value if cls.doc_type else None,
        "title": cls.title_normalized,
        "sensitivity_level": gov.sensitivity_level.value if gov.sensitivity_level else None,
        "contains_pii": gov.contains_pii,
        "owner": gov.owner,
        "access_groups": list(gov.access_groups),
        "lifecycle_status": life.status.value if life.status else None,
        "effective_date": life.effective_date,
        "expiry_date": life.expiry_date,
        "superseded_by": life.superseded_by,
        "metadata_json": doc.model_dump(mode="json"),
    }


def row_to_document(row: Document) -> DocumentMetadata:
    return DocumentMetadata.model_validate(row.metadata_json)
