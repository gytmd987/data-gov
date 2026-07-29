"""색인된 문서의 Qdrant payload 재적용(백필).

payload 스키마에 필드가 추가됐을 때(예: 폴더 검색용 author_node_id) 기존 문서에도
반영하기 위해 사용한다. 벡터는 그대로 두고 payload 만 갱신하므로 빠르다.

    python -m scripts.reindex_payloads
"""

from __future__ import annotations

from app.db.repositories import DocumentRepository
from app.db.session import make_engine
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import doc_level_payload


def main() -> int:
    from sqlalchemy.orm import Session

    from app.review.factory import build_document_manager

    engine = make_engine()
    with Session(engine) as session:
        mgr = build_document_manager(session)
        repo = DocumentRepository(session)
        doc_ids = repo.list_by_status(IngestionStatus.INDEXED)
        n_ok = 0
        for doc_id in doc_ids:
            doc = repo.get(doc_id)
            if doc is None or mgr.indexer is None:
                continue
            try:
                mgr.indexer.set_doc_payload(doc_id, doc_level_payload(doc))
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                print(f"  실패: {doc_id} ({e})")
        print(f"payload 재적용 완료: {n_ok}/{len(doc_ids)}건")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
