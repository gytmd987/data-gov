"""하이브리드 결과 융합 (Reciprocal Rank Fusion).

dense(벡터)와 sparse(BM25) 각각의 순위 리스트를 RRF로 결합한다.
RRF score = Σ 1 / (k + rank_i). 스코어 스케일이 다른 두 검색을 순위 기반으로 안전하게 합친다.
"""

from __future__ import annotations

from typing import Iterable

from .types import RetrievedChunk

DEFAULT_RRF_K = 60


def reciprocal_rank_fusion(
    rankings: Iterable[list[RetrievedChunk]],
    k: int = DEFAULT_RRF_K,
) -> list[RetrievedChunk]:
    """여러 순위 리스트를 RRF로 결합. chunk_id 기준으로 중복을 합산한다."""
    scores: dict[str, float] = {}
    repr_chunk: dict[str, RetrievedChunk] = {}

    for ranking in rankings:
        for rank, chunk in enumerate(ranking):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            # 텍스트/payload 보존(먼저 등장한 것 유지)
            repr_chunk.setdefault(chunk.chunk_id, chunk)

    fused = [
        repr_chunk[cid].model_copy(update={"score": score})
        for cid, score in scores.items()
    ]
    fused.sort(key=lambda c: c.score, reverse=True)
    return fused
