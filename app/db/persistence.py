"""적재 파이프라인 ↔ 영속 계층 연결 헬퍼.

파이프라인은 상태를 IngestionContext(메모리)로 다루므로, 각 단계 후 이 헬퍼로 영속화한다.
  - save_ingestion(repo, ctx): 문서·청크·상태를 저장(각 전이 후 호출).
  - persistent_hash_lookup(repo): intake에 주입할 중복 탐지 콜백.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from app.db.models import Chunk as ChunkRow
from app.db.models import Document as DocumentRow
from app.db.repositories import DocumentRepository
from app.ingestion.chunking import Chunk
from app.ingestion.pipeline import IngestionContext
from app.schemas.enums import ChunkType
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import ChunkMetadata


def persistent_hash_lookup(repo: DocumentRepository):
    return repo.hash_lookup


def save_ingestion(repo: DocumentRepository, ctx: IngestionContext) -> None:
    """현재 컨텍스트 상태를 영속화한다."""
    repo.upsert_document(ctx.doc, ctx.status)
    if ctx.chunks:
        repo.upsert_chunks(ctx.doc.identification.doc_id, ctx.chunks)
    if ctx.status == IngestionStatus.INDEXED:
        repo.mark_chunks_indexed(ctx.doc.identification.doc_id)


def load_context(repo: DocumentRepository, doc_id: str) -> Optional[IngestionContext]:
    """DB에서 IngestionContext를 복원한다(Streamlit rerun 간 상태 유지용)."""
    doc = repo.get(doc_id)
    if doc is None:
        return None
    doc_row = repo.session.get(DocumentRow, doc_id)
    status = IngestionStatus(doc_row.status)

    chunk_rows = repo.session.execute(
        select(ChunkRow).where(ChunkRow.parent_doc_id == doc_id)
        .order_by(ChunkRow.chunk_id)
    ).scalars()
    chunks = [
        Chunk(
            meta=ChunkMetadata(
                chunk_id=r.chunk_id,
                parent_doc_id=r.parent_doc_id,
                chunk_type=ChunkType(r.chunk_type),
                section_title=r.section_title,
                page_no=r.page_no,
            ),
            text=r.text,
        )
        for r in chunk_rows
    ]
    return IngestionContext(doc=doc, chunks=chunks, status=status)
