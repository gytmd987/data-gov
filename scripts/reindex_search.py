"""검색 인덱스(Qdrant) 재구축 — 의미(dense) + 어휘(BM25) 두 축.

언제 쓰나:
- **하이브리드(BM25) 검색을 처음 켤 때** — 기존 컬렉션에는 어휘 벡터가 없다.
- 임베딩 모델을 바꿨을 때.
- 제목·파일명 검색 청크 등 색인 텍스트 구성이 바뀌었을 때.

    python -m scripts.reindex_search --recreate     # 컬렉션 다시 만들고 전체 재색인
    python -m scripts.reindex_search --sparse-only  # 어휘 벡터만 채움(임베딩 없음, 빠름)
    python -m scripts.reindex_search --limit 20     # 일부만(확인용)

청크 본문은 PostgreSQL 에 있으므로 파일을 다시 읽거나 LLM 을 호출하지 않는다.
--sparse-only 는 임베딩도 안 하지만, **컬렉션에 이미 어휘 벡터 설정이 있어야** 한다
(없으면 --recreate 가 필요하다고 알려준다).
"""

from __future__ import annotations

import argparse

from app.clients.qdrant_indexer import QdrantIndexer
from app.config import settings
from app.db.repositories import DocumentRepository
from app.db.session import make_engine
from app.ingestion.pipeline import build_qa_chunk_text
from app.schemas.enums import ChunkType
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import ChunkMetadata, doc_level_payload
from app.search.lexical import to_qdrant_sparse

_SCROLL = 256


def _has_sparse(client, collection: str, name: str) -> bool:
    try:
        info = client.get_collection(collection)
        return name in (getattr(info.config.params, "sparse_vectors", None) or {})
    except Exception:      # noqa: BLE001
        return False


def sparse_only(indexer: QdrantIndexer, limit: int = 0) -> int:
    """이미 저장된 청크 본문으로 어휘 벡터만 만들어 넣는다(임베딩 호출 없음)."""
    from qdrant_client import models as qm

    client, coll = indexer.client, indexer.collection
    total = done = 0
    offset = None
    while True:
        points, offset = client.scroll(collection_name=coll, limit=_SCROLL,
                                       offset=offset, with_payload=True,
                                       with_vectors=False)
        if not points:
            break
        updates = []
        for p in points:
            total += 1
            text = (p.payload or {}).get("text") or ""
            if text.strip():
                updates.append(qm.PointVectors(
                    id=p.id,
                    vector={indexer.SPARSE_NAME: to_qdrant_sparse(text)}))
        if updates:
            client.update_vectors(collection_name=coll, points=updates)
            done += len(updates)
        print(f"  … {total}개 처리(갱신 {done})", flush=True)
        if offset is None or (limit and total >= limit):
            break
    return done


def rebuild(session, indexer: QdrantIndexer, embedder, limit: int = 0) -> tuple[int, int]:
    """DB의 청크로 문서 전체를 다시 색인한다. → (문서 수, 청크 수)"""
    repo = DocumentRepository(session)
    doc_ids = repo.list_by_status(IngestionStatus.INDEXED)
    if limit:
        doc_ids = doc_ids[:limit]
    n_doc = n_chunk = 0
    for i, doc_id in enumerate(doc_ids, start=1):
        doc = repo.get(doc_id)
        rows = repo.chunks_of(doc_id)
        if doc is None or not rows:
            continue
        texts, payloads, ids = [], [], []
        for r in rows:
            meta = ChunkMetadata(chunk_id=r["chunk_id"], parent_doc_id=doc_id,
                                 chunk_type=ChunkType(r["chunk_type"]),
                                 section_title=r["section_title"],
                                 page_no=r["page_no"])
            payload = meta.to_qdrant_payload(doc)
            payload["text"] = r["text"]
            texts.append(r["text"]); payloads.append(payload); ids.append(r["chunk_id"])

        # 제목·파일명으로도 찾을 수 있게 하는 문서 단위 합성 청크
        synth = build_qa_chunk_text(doc)
        if synth:
            qa = doc_level_payload(doc)
            qa.update({"chunk_id": f"{doc_id}::qa", "parent_doc_id": doc_id,
                       "chunk_type": ChunkType.QA.value, "section_title": None,
                       "page_no": None, "text": synth})
            texts.append(synth); payloads.append(qa); ids.append(f"{doc_id}::qa")

        indexer.upsert(embedder.embed(texts), payloads, ids)
        n_doc += 1
        n_chunk += len(texts)
        if i % 50 == 0:
            print(f"  … 문서 {i}/{len(doc_ids)} · 청크 {n_chunk}개", flush=True)
    return n_doc, n_chunk


def main(argv=None) -> int:
    from sqlalchemy.orm import Session

    ap = argparse.ArgumentParser(description="검색 인덱스 재구축(dense + BM25)")
    ap.add_argument("--recreate", action="store_true",
                    help="컬렉션을 지우고 다시 만든다(어휘 검색을 처음 켤 때 필요)")
    ap.add_argument("--sparse-only", action="store_true",
                    help="어휘 벡터만 채운다(임베딩 호출 없음)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--collection", default="hr_chunks")
    args = ap.parse_args(argv)

    indexer = QdrantIndexer(collection=args.collection,
                            vector_size=settings.embedding_dim)
    client = indexer.client

    if args.recreate and client.collection_exists(args.collection):
        print(f"컬렉션 삭제: {args.collection}")
        client.delete_collection(args.collection)
    indexer.ensure_collection()

    if args.sparse_only:
        if not _has_sparse(client, args.collection, indexer.SPARSE_NAME):
            print("⛔ 이 컬렉션에는 어휘 검색 벡터 설정이 없습니다.")
            print("   기존 컬렉션에는 나중에 추가할 수 없으니 전체 재색인이 필요합니다:")
            print("     python -m scripts.reindex_search --recreate")
            return 2
        n = sparse_only(indexer, args.limit)
        print(f"어휘 벡터 백필 완료: {n}개 청크")
        return 0

    from app.clients.embedding import TEIEmbedder
    engine = make_engine()
    with Session(engine) as session:
        n_doc, n_chunk = rebuild(session, indexer, TEIEmbedder(), args.limit)
    print(f"재색인 완료: 문서 {n_doc}건 · 청크 {n_chunk}개")
    print("이제 사내 조어·조항 번호·문서코드·파일명도 어휘 검색으로 잡힙니다.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
