"""Qdrant 검색 어댑터 (dense + sparse/BM25).

- QdrantDenseSearch: 질의를 KURE-v1로 임베딩해 벡터 검색(하드필터 주입).
- QdrantBM25Search: Qdrant 내장 BM25(로컬 추론)로 어휘 검색.

두 어댑터 모두 retriever.DenseSearch / SparseSearch 프로토콜을 구현한다.
sparse 인덱스 구성(BM25) 등 컬렉션 스키마는 인덱싱 단계에서 준비한다(TODO: 인덱서 확장).
"""

from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app.clients.embedding import TEIEmbedder
from app.config import settings
from app.search.types import RetrievedChunk


def _to_chunk(point) -> RetrievedChunk:
    payload = point.payload or {}
    return RetrievedChunk(
        chunk_id=payload.get("chunk_id", str(point.id)),
        text=payload.get("text", ""),
        score=float(point.score) if getattr(point, "score", None) is not None else 0.0,
        payload=payload,
    )


class QdrantDenseSearch:
    def __init__(self, embedder, collection: str = "hr_chunks",
                 host: str = "localhost", port: int | None = None,
                 client: QdrantClient | None = None) -> None:
        self._embedder = embedder
        self.collection = collection
        self.client = client or QdrantClient(
            host=host, port=port or settings.qdrant_http_port)

    def search_dense(self, query: str, top_n: int, qdrant_filter: Any) -> list[RetrievedChunk]:
        vector = self._embedder.embed([query])[0]
        res = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=qdrant_filter,
            limit=top_n,
            with_payload=True,
        )
        return [_to_chunk(p) for p in res.points]


class QdrantBM25Search:
    """Qdrant 내장 BM25(sparse) 검색. 컬렉션에 'bm25' 스파스 벡터가 구성돼 있어야 한다."""

    def __init__(self, collection: str = "hr_chunks",
                 host: str = "localhost", port: int | None = None) -> None:
        self.collection = collection
        self.client = QdrantClient(host=host, port=port or settings.qdrant_http_port)

    def search_sparse(self, query: str, top_n: int, qdrant_filter: Any) -> list[RetrievedChunk]:
        res = self.client.query_points(
            collection_name=self.collection,
            query=qm.Document(text=query, model="Qdrant/bm25"),
            using="bm25",
            query_filter=qdrant_filter,
            limit=top_n,
            with_payload=True,
        )
        return [_to_chunk(p) for p in res.points]
