"""리랭킹 (bge-reranker-v2-m3, TEI /rerank).

하이브리드 topN 후보를 질문과의 관련도로 재정렬하고 상위 topK를 반환한다.
Reranker는 Protocol로 주입 → 서비스 없이 테스트 가능.
"""

from __future__ import annotations

from typing import Protocol

from .types import RetrievedChunk


class Reranker(Protocol):
    def rerank(self, query: str, texts: list[str]) -> list[float]:
        """각 text의 관련도 점수를 texts와 같은 순서로 반환."""
        ...


def rerank_chunks(
    reranker: Reranker,
    query: str,
    chunks: list[RetrievedChunk],
    top_k: int = 6,
) -> list[RetrievedChunk]:
    if not chunks:
        return []
    scores = reranker.rerank(query, [c.text for c in chunks])
    scored = [
        c.model_copy(update={"score": s}) for c, s in zip(chunks, scores)
    ]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:top_k]
