"""검색·답변 오케스트레이션.

    질문 + 사용자 → 하이브리드 검색(하드필터) → 리랭킹 → 답변 생성(근거·인용)
                 → 감사로그 기록 → 응답

감사로그는 AuditSink Protocol로 주입한다(구현은 Postgres append-only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .access import AccessPolicy, UserContext
from .answer import TextLLM, generate_answer
from .rerank import Reranker, rerank_chunks
from .retriever import HybridRetriever
from .types import Answer


class AuditSink(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...


@dataclass
class SearchPipeline:
    retriever: HybridRetriever
    reranker: Reranker
    llm: TextLLM
    audit: Optional[AuditSink] = None
    top_n: int = 40
    top_k: int = 6

    def answer(self, query: str, user: UserContext, today=None) -> Answer:
        policy = AccessPolicy.for_user(user, today=today)

        candidates = self.retriever.retrieve(query, policy, top_n=self.top_n)
        reranked = rerank_chunks(self.reranker, query, candidates, top_k=self.top_k)

        # 방어적 최종 재검증(인용 직전)
        reranked = [c for c in reranked if policy.allows(c.payload)]

        result = generate_answer(self.llm, query, reranked)

        if self.audit is not None:
            self.audit.record({
                "action": "query",
                "user_id": user.user_id,
                "query_text": query,
                "retrieved_chunk_ids": [c.chunk_id for c in candidates],
                "cited_chunk_ids": [c.chunk_id for c in result.citations],
                "cited_doc_ids": [c.doc_id for c in result.citations],
            })
        return result
