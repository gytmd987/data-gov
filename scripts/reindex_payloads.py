"""색인된 문서의 Qdrant payload 재적용 + 문서 단위 검색 청크 갱신(백필).

    python -m scripts.reindex_payloads          # payload 만 (빠름)
    python -m scripts.reindex_payloads --qa     # + 제목·파일명 검색 청크 재생성

payload 는 벡터를 건드리지 않아 즉시 끝난다(예: 폴더 검색용 author_node_id 추가 시).
--qa 는 문서마다 합성 Q&A 청크(제목·파일명·요약·키워드·예상 Q&A)를 다시 만들어
임베딩한다. **제목·파일명으로도 검색되게** 하려면 기존 문서에 한 번 돌려야 한다.
LLM 은 쓰지 않고 임베딩만 1회씩 하므로 대량이어도 부담이 적다.
"""

from __future__ import annotations

import argparse

from app.db.repositories import DocumentRepository
from app.db.session import make_engine
from app.ingestion.pipeline import build_qa_chunk_text
from app.schemas.enums import ChunkType
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import doc_level_payload


def refresh_qa_chunk(doc, embedder, indexer) -> bool:
    """이 문서의 합성 Q&A 청크를 다시 만들어 업서트한다. 내용이 없으면 건너뜀."""
    text = build_qa_chunk_text(doc)
    if not text:
        return False
    doc_id = doc.identification.doc_id
    payload = doc_level_payload(doc)
    payload.update({"chunk_id": f"{doc_id}::qa", "parent_doc_id": doc_id,
                    "chunk_type": ChunkType.QA.value, "section_title": None,
                    "page_no": None, "text": text})
    vectors = embedder.embed([text])
    indexer.upsert(vectors, [payload], [f"{doc_id}::qa"])
    return True


def main(argv=None) -> int:
    from sqlalchemy.orm import Session

    from app.review.factory import build_document_manager

    ap = argparse.ArgumentParser(description="Qdrant payload/검색청크 백필")
    ap.add_argument("--qa", action="store_true",
                    help="제목·파일명이 검색에 잡히도록 문서 단위 검색 청크도 재생성")
    args = ap.parse_args(argv)

    embedder = None
    if args.qa:
        from app.clients.embedding import TEIEmbedder
        embedder = TEIEmbedder()

    engine = make_engine()
    with Session(engine) as session:
        mgr = build_document_manager(session)
        repo = DocumentRepository(session)
        doc_ids = repo.list_by_status(IngestionStatus.INDEXED)
        n_ok = n_qa = 0
        for i, doc_id in enumerate(doc_ids, start=1):
            doc = repo.get(doc_id)
            if doc is None or mgr.indexer is None:
                continue
            try:
                mgr.indexer.set_doc_payload(doc_id, doc_level_payload(doc))
                n_ok += 1
                if embedder is not None and refresh_qa_chunk(doc, embedder, mgr.indexer):
                    n_qa += 1
            except Exception as e:  # noqa: BLE001 — 한 건 실패로 전체를 멈추지 않는다
                print(f"  실패: {doc_id} ({e})")
            if i % 200 == 0:
                print(f"  … {i}/{len(doc_ids)}건 처리")
        print(f"payload 재적용 완료: {n_ok}/{len(doc_ids)}건"
              + (f" · 검색 청크 갱신 {n_qa}건" if embedder is not None else ""))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
