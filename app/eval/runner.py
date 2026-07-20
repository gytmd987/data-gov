"""평가 러너: 골드셋을 파이프라인에 돌려 검색 지표·접근통제·판정 점수를 집계.

- 검색 지표: 최종 노출 청크(used_chunks)의 source_filename 순위로 Recall@k / MRR / nDCG@k.
- 접근통제 회귀(보안): forbidden_filenames 가 노출/인용되면 위반. 하나라도 있으면 실패.
- LLM-as-judge(선택): groundedness / relevance 평균.

파이프라인은 answer(query, user, today)->Answer 를 제공하는 객체면 된다(SearchPipeline 또는 fake).
user_resolver(user_id)->UserContext 로 질의 주체를 만든다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

from app.eval.goldset import GoldItem, GoldSet
from app.eval.judge import JudgeLLM, judge_answer
from app.eval.metrics import dedup_preserve_order, ndcg_at_k, recall_at_k, reciprocal_rank
from app.search.access import UserContext

UserResolver = Callable[[str], Optional[UserContext]]


@dataclass
class ItemResult:
    id: str
    query: str
    user_id: str
    ranked_filenames: list[str]
    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    answer_ok: bool                        # expected_answer_contains 충족
    access_violations: list[str] = field(default_factory=list)
    groundedness: Optional[float] = None
    relevance: Optional[float] = None


@dataclass
class EvalReport:
    results: list[ItemResult]
    k: int

    @property
    def total_access_violations(self) -> int:
        return sum(len(r.access_violations) for r in self.results)

    def _mean(self, attr: str) -> float:
        vals = [getattr(r, attr) for r in self.results if getattr(r, attr) is not None]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def mean_recall(self) -> float:
        return self._mean("recall_at_k")

    @property
    def mean_mrr(self) -> float:
        return self._mean("mrr")

    @property
    def mean_ndcg(self) -> float:
        return self._mean("ndcg_at_k")

    @property
    def answer_accuracy(self) -> float:
        return sum(1 for r in self.results if r.answer_ok) / len(self.results) if self.results else 0.0

    @property
    def mean_groundedness(self) -> float:
        return self._mean("groundedness")

    @property
    def mean_relevance(self) -> float:
        return self._mean("relevance")

    @property
    def passed(self) -> bool:
        """보안 회귀: 접근통제 위반이 하나도 없어야 통과."""
        return self.total_access_violations == 0


def _evaluate_item(
    pipeline, item: GoldItem, user: UserContext, k: int,
    judge: Optional[JudgeLLM], today: date,
) -> ItemResult:
    answer = pipeline.answer(item.query, user, today=today)

    used = answer.used_chunks
    ranked = dedup_preserve_order(
        [c.source_filename for c in used if c.source_filename])
    cited_files = {c.title for c in answer.citations}  # title=파일 제목일 수 있음(참고)

    # 접근통제 위반: 금지 문서가 노출(used) 또는 인용됐는가
    exposed = set(ranked)
    violations = [f for f in item.forbidden_filenames if f in exposed]
    # 인용 문서까지 검사(파일명 기준)
    cited_filenames = {c.source_filename for c in used
                       if any(cit.chunk_id == c.chunk_id for cit in answer.citations)}
    violations += [f for f in item.forbidden_filenames
                   if f in cited_filenames and f not in violations]

    answer_ok = all(s in answer.text for s in item.expected_answer_contains)

    result = ItemResult(
        id=item.id, query=item.query, user_id=item.user_id,
        ranked_filenames=ranked,
        recall_at_k=recall_at_k(ranked, item.relevant_filenames, k),
        mrr=reciprocal_rank(ranked, item.relevant_filenames),
        ndcg_at_k=ndcg_at_k(ranked, item.relevant_filenames, k),
        answer_ok=answer_ok,
        access_violations=violations,
    )

    if judge is not None:
        score = judge_answer(judge, item.query, answer.text,
                             [c.text for c in used])
        result.groundedness = score.groundedness
        result.relevance = score.relevance

    return result


def run_eval(
    pipeline,
    goldset: GoldSet,
    user_resolver: UserResolver,
    k: int = 5,
    judge: Optional[JudgeLLM] = None,
    today: Optional[date] = None,
) -> EvalReport:
    today = today or date.today()
    results: list[ItemResult] = []
    for item in goldset.items:
        user = user_resolver(item.user_id)
        if user is None:
            raise ValueError(f"알 수 없는 사용자: {item.user_id}")
        results.append(_evaluate_item(pipeline, item, user, k, judge, today))
    return EvalReport(results=results, k=k)
