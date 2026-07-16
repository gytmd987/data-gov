"""적재 파이프라인 ↔ 영속 계층 연결 헬퍼.

파이프라인은 상태를 IngestionContext(메모리)로 다루므로, 각 단계 후 이 헬퍼로 영속화한다.
  - save_ingestion(repo, ctx): 문서·청크·상태를 저장(각 전이 후 호출).
  - persistent_hash_lookup(repo): intake에 주입할 중복 탐지 콜백.
"""

from __future__ import annotations

from app.db.repositories import DocumentRepository
from app.ingestion.pipeline import IngestionContext
from app.schemas.ingestion import IngestionStatus


def persistent_hash_lookup(repo: DocumentRepository):
    return repo.hash_lookup


def save_ingestion(repo: DocumentRepository, ctx: IngestionContext) -> None:
    """현재 컨텍스트 상태를 영속화한다."""
    repo.upsert_document(ctx.doc, ctx.status)
    if ctx.chunks:
        repo.upsert_chunks(ctx.doc.identification.doc_id, ctx.chunks)
    if ctx.status == IngestionStatus.INDEXED:
        repo.mark_chunks_indexed(ctx.doc.identification.doc_id)
