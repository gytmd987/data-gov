"""리랭킹 (bge-reranker-v2-m3, TEI /rerank).

하이브리드 topN 후보를 질문과의 관련도로 재정렬하고 상위 topK를 반환한다.
Reranker는 Protocol로 주입 → 서비스 없이 테스트 가능.
"""

from __future__ import annotations

from typing import Optional, Protocol

from .types import RetrievedChunk


class Reranker(Protocol):
    def rerank(self, query: str, texts: list[str]) -> list[float]:
        """각 text의 관련도 점수를 texts와 같은 순서로 반환."""
        ...


def rerank_chunks(
    reranker: Optional[Reranker],
    query: str,
    chunks: list[RetrievedChunk],
    top_k: int = 6,
) -> list[RetrievedChunk]:
    """후보를 질문과의 관련도로 재정렬하고 상위 top_k 를 반환.

    reranker=None 이면 **재정렬하지 않고** 들어온 순서(하이브리드 검색 융합 순위)
    그대로 상위 top_k 를 준다. 리랭커가 CPU 로 돌아 질문마다 8초씩 걸릴 때의
    탈출구다 — 정확도는 조금 떨어지지만 답이 바로 나온다.
    """
    if not chunks:
        return []
    if reranker is None:
        return chunks[:top_k]
    scores = reranker.rerank(query, [c.text for c in chunks])
    scored = [
        c.model_copy(update={"score": s}) for c, s in zip(chunks, scores)
    ]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:top_k]
