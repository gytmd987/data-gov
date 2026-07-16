"""하이브리드 검색기.

dense(KURE-v1 임베딩) + sparse(Qdrant 내장 BM25)를 각각 실행하고 RRF로 융합한다.
접근통제 하드필터는 두 단계로 적용:
  1) Qdrant 쿼리에 AccessPolicy.to_qdrant_filter() 주입(후보 단계 배제)
  2) 융합 후 AccessPolicy.allows()로 재검증(만료/대체 확정 배제, 방어적 이중 체크)
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from .access import AccessPolicy
from .fusion import reciprocal_rank_fusion
from .types import RetrievedChunk


class DenseSearch(Protocol):
    def search_dense(
        self, query: str, top_n: int, qdrant_filter: Any
    ) -> list[RetrievedChunk]: ...


class SparseSearch(Protocol):
    def search_sparse(
        self, query: str, top_n: int, qdrant_filter: Any
    ) -> list[RetrievedChunk]: ...


class HybridRetriever:
    def __init__(self, dense: DenseSearch, sparse: Optional[SparseSearch] = None) -> None:
        self._dense = dense
        self._sparse = sparse

    def retrieve(
        self, query: str, policy: AccessPolicy, top_n: int = 40
    ) -> list[RetrievedChunk]:
        qfilter = policy.to_qdrant_filter()

        rankings = [self._dense.search_dense(query, top_n, qfilter)]
        if self._sparse is not None:
            rankings.append(self._sparse.search_sparse(query, top_n, qfilter))

        fused = reciprocal_rank_fusion(rankings)

        # 방어적 재검증: 만료/대체/권한/민감도 재확인
        allowed = [c for c in fused if policy.allows(c.payload)]
        return allowed[:top_n]
