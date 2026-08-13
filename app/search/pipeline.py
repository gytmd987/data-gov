"""검색·답변 오케스트레이션.

    질문 + 사용자 → 하이브리드 검색(하드필터) → 리랭킹 → 답변 생성(근거·인용)
                 → 감사로그 기록 → 응답

감사로그는 AuditSink Protocol로 주입한다(구현은 Postgres append-only).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol

from .access import AccessPolicy, UserContext
from .answer import TextLLM, finalize_answer, generate_answer, stream_answer
from .rerank import Reranker, rerank_chunks
from .retriever import HybridRetriever
from .types import Answer


# 표(SQL) 경로로 넘길 상위 문서 수. 넓히면 질문과 무관한 표가 답을 가로챈다.
PREEMPT_TOP_DOCS = 3


class AuditSink(Protocol):
    def record(self, event: dict[str, Any]) -> None: ...


@dataclass
class SearchPipeline:
    retriever: HybridRetriever
    reranker: Optional[Reranker]     # None 이면 재정렬 없이 검색 융합 순위를 쓴다
    llm: TextLLM
    audit: Optional[AuditSink] = None
    top_n: int = 40
    top_k: int = 6
    # 판단 루프가 여러 도구로 모은 근거를 답변 프롬프트에 넣을 때의 상한.
    # top_k(6)보다 넉넉해야 "규정 12건을 훑어봤다" 같은 답이 되고, 너무 크면
    # 프롬프트가 길어져 느려지고 모델이 핵심을 놓친다.
    max_evidence: int = 14

    def answer(self, query: str, user: UserContext, today=None,
               include_past: bool = False, folder_node_ids=None) -> Answer:
        policy = AccessPolicy.for_user(
            user, today=today, include_past=include_past,
            folder_node_ids=frozenset(folder_node_ids) if folder_node_ids else None)

        candidates = self.retriever.retrieve(query, policy, top_n=self.top_n)
        reranked = rerank_chunks(self.reranker, query, candidates, top_k=self.top_k)

        # 방어적 최종 재검증(인용 직전)
        reranked = [c for c in reranked if policy.allows(c.payload)]

        result = generate_answer(self.llm, query, reranked)
        self._audit(user, query, candidates, result)
        return result

    # ── 판단 루프 + 스트리밍 ─────────────────────────────────────────────────
    def answer_events(self, query: str, user: UserContext, session, today=None,
                      include_past: bool = False, folder_node_ids=None,
                      plan: bool = True, preempt=None):
        """이벤트 생성기 — 화면이 진행 상황과 답변 조각을 바로 받아 볼 수 있게 한다.

        내보내는 이벤트:
          {"type":"step",   "text": "규정 47건 확인"}   진행 상황(무엇을 찾고 있나)
          {"type":"delta",  "text": "연차는 "}          답변 조각(도착하는 대로)
          {"type":"answer", "answer": Answer}           후처리까지 끝난 최종본
          {"type":"preempted", "result": ...}           다른 방식으로 답했음(아래)

        plan=False 면 판단 루프 없이 기존 경로(검색 1회)로 간다.

        preempt(doc_ids) 를 주면 **답변을 생성하기 직전에** 한 번 물어본다. 값을
        돌려주면 문장 생성을 아예 건너뛴다. 표 데이터(엑셀)처럼 SQL 로 정확히 답할 수
        있는 질문에 쓴다 — 안 그러면 문장 답변을 다 만들어 놓고 버리게 되어, 사용자는
        답이 흘러나오는 걸 지켜본 뒤 전혀 다른 답으로 바뀌는 걸 보게 된다.
        """
        from .tools import ToolBox

        scope = frozenset(folder_node_ids) if folder_node_ids else None
        policy = AccessPolicy.for_user(user, today=today, include_past=include_past,
                                       folder_node_ids=scope)

        # 첫 검색은 미리 돌려 둔다 — 대부분의 질문은 이걸로 바로 답한다(왕복 절약)
        candidates = self.retriever.retrieve(query, policy, top_n=self.top_n)
        reranked = [c for c in rerank_chunks(self.reranker, query, candidates,
                                             top_k=self.top_k)
                    if policy.allows(c.payload)]

        steps: list = []
        evidence = reranked
        if plan and session is not None:
            box = ToolBox(session=session, policy=policy,
                          visibility=_visibility_of(user),
                          retrieve=self._retrieve_ranked, today=policy.today,
                          folder_node_ids=scope)
            # 한 걸음마다 진행 상황을 그대로 흘려보낸다(다 끝나고 주면 멈춘 듯 보인다)
            outcome = yield from _plan_events(self.llm, query, box, reranked, steps)
            if outcome.evidence:
                evidence = _merge_evidence(reranked, outcome.evidence,
                                           self.max_evidence)

        # 답변에 넣기 직전 마지막 재검증 — 이 경로로도 권한 밖 문서가 새면 안 된다
        evidence = [c for c in evidence if policy.allows(c.payload)]

        # 문장을 만들기 **전에** 물어본다 — 만들고 나서 버리면 그 시간이 통째로 낭비다.
        # **상위 몇 건만** 넘긴다. 후보 전체를 넘기면 순위가 한참 낮은 표 하나 때문에
        # 엉뚱한 질문까지 SQL 경로로 가로채인다(질문과 무관한 표가 답이 되어 버린다).
        if preempt is not None:
            top = []
            for c in evidence:
                if c.doc_id and c.doc_id not in top:
                    top.append(c.doc_id)
                if len(top) >= PREEMPT_TOP_DOCS:
                    break
            try:
                taken = preempt(top)
            except Exception:      # 대체 경로가 깨져도 평소대로 답해야 한다
                taken = None
            if taken is not None:
                self._audit(user, query, evidence, Answer(text=""), steps=steps)
                yield {"type": "preempted", "result": taken}
                return

        raw = ""
        for piece in stream_answer(self.llm, query, evidence):
            raw += piece
            yield {"type": "delta", "text": piece}

        result = finalize_answer(raw, evidence)
        self._audit(user, query, evidence, result, steps=steps)
        yield {"type": "answer", "answer": result}

    def _retrieve_ranked(self, query: str, policy: AccessPolicy,
                         top_n: int) -> list:
        """도구용 검색 — 검색 + 리랭킹까지(도구가 쓰기 좋은 순서로)."""
        found = self.retriever.retrieve(query, policy, top_n=max(top_n, self.top_n))
        ranked = rerank_chunks(self.reranker, query, found, top_k=top_n)
        return [c for c in ranked if policy.allows(c.payload)]

    def _audit(self, user, query, candidates, result, steps=None) -> None:
        if self.audit is None:
            return
        event = {
            "action": "query",
            "user_id": user.user_id,
            "query_text": query,
            "retrieved_chunk_ids": [c.chunk_id for c in candidates],
            "cited_chunk_ids": [c.chunk_id for c in result.citations],
            "cited_doc_ids": [c.doc_id for c in result.citations],
        }
        if steps:
            # 판단 루프가 무엇을 뒤졌는지도 남긴다 — 최종 질문만으로는 추적이 안 된다
            event["tool_calls"] = [{"tool": s.tool, "reason": s.reason} for s in steps]
        self.audit.record(event)


def _plan_events(llm, query, box, prefetched, steps):
    """판단 루프를 돌리며 진행 상황을 이벤트로 내보낸다.

    **어떤 실패에도 예외를 올리지 않는다.** 판단이 깨지면 미리 돌려 둔 검색 결과로
    답한다 = 지금까지의 동작. 새 기능 때문에 답이 아예 안 나오는 일은 없어야 한다.
    """
    from .agent import AgentResult, run_agent
    from .tools import ToolResult

    seed = ToolResult(text="\n".join(
        f"- {c.title or c.source_filename}: {(c.text or '')[:300]}" for c in prefetched),
        chunks=list(prefetched))
    try:
        walker = run_agent(llm, query, box, prefetch=seed)
        while True:
            try:
                step = next(walker)
            except StopIteration as stop:
                return stop.value or AgentResult()
            steps.append(step)
            yield {"type": "step", "text": step.note}
    except Exception:
        return AgentResult()


def _merge_evidence(prefetched, found, limit: int):
    """미리 찾은 것 + 판단 루프가 모은 것. 순서는 유지하고 중복만 제거한다."""
    out, seen = [], set()
    for c in list(found) + list(prefetched):
        if c.chunk_id in seen:
            continue
        seen.add(c.chunk_id)
        out.append(c)
    return out[:limit]


def _visibility_of(user: UserContext):
    """조직 토큰 → 관리 화면용 가시 범위(문서 목록·세기 도구가 쓴다).

    채팅 사용자는 부서장 권한으로 넓히지 않는다 — 읽을 수 있는 것만 본다.
    """
    from .access import Visibility
    return Visibility(read_tokens=frozenset(user.groups))
