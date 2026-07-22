"""평가 하네스 테스트 (오프라인, fake 파이프라인)."""

from datetime import date

from app.eval.goldset import GoldItem, GoldSet
from app.eval.judge import judge_answer
from app.eval.metrics import ndcg_at_k, recall_at_k, reciprocal_rank
from app.eval.runner import run_eval
from app.search.access import UserContext
from app.search.types import Answer, Citation, RetrievedChunk


# ── 지표 ─────────────────────────────────────────────────────────────────────
def test_recall_mrr_ndcg():
    ranked = ["a", "b", "c", "d"]
    assert recall_at_k(ranked, ["b", "e"], k=2) == 0.5      # b는 top2, e 없음
    assert reciprocal_rank(ranked, ["c"]) == 1 / 3
    assert reciprocal_rank(ranked, ["z"]) == 0.0
    assert ndcg_at_k(ranked, ["a"], k=4) == 1.0             # 1위 정답 → 완벽
    assert 0 < ndcg_at_k(ranked, ["c"], k=4) < 1.0


# ── judge (fake) ─────────────────────────────────────────────────────────────
class FakeJudge:
    def complete_json(self, prompt, schema):
        return {"groundedness": 0.9, "relevance": 0.8}


def test_judge_answer():
    s = judge_answer(FakeJudge(), "연차?", "연차는 15일", ["연차 15일"])
    assert s.groundedness == 0.9 and s.relevance == 0.8


# ── fake 파이프라인 ──────────────────────────────────────────────────────────
def _chunk(fn, text="본문", cid=None):
    cid = cid or f"{fn}::0"
    return RetrievedChunk(chunk_id=cid, text=text, score=1.0,
                          payload={"source_filename": fn, "parent_doc_id": fn,
                                   "title": fn})


def _answer(text, chunks, cite_first=True):
    cites = []
    if cite_first and chunks:
        c = chunks[0]
        cites = [Citation(marker=1, chunk_id=c.chunk_id, title=c.title,
                          doc_id=c.doc_id, page_no=1)]
    return Answer(text=text, citations=cites, used_chunks=chunks)


class FakePipeline:
    def __init__(self, by_query):
        self.by_query = by_query

    def answer(self, query, user, today=None):
        return self.by_query[query]


def _resolver(user_id):
    return UserContext(user_id, frozenset(["n:1"]))


def test_run_eval_metrics_and_answer_ok():
    gold = GoldSet(items=[
        GoldItem(id="leave", query="연차 며칠?", user_id="u1",
                 relevant_filenames=["leave.txt"],
                 forbidden_filenames=["salary.txt"],
                 expected_answer_contains=["15"]),
    ])
    pipe = FakePipeline({
        "연차 며칠?": _answer("연차는 15일입니다 [1].", [_chunk("leave.txt")]),
    })
    report = run_eval(pipe, gold, _resolver, k=5)
    r = report.results[0]
    assert r.recall_at_k == 1.0 and r.mrr == 1.0
    assert r.answer_ok is True
    assert report.passed                       # 위반 없음
    assert report.total_access_violations == 0


def test_run_eval_detects_access_violation():
    # 금지 문서(salary.txt)가 결과에 노출 → 위반, 회귀 실패
    gold = GoldSet(items=[
        GoldItem(id="leak", query="부장 연봉?", user_id="u1",
                 relevant_filenames=[],
                 forbidden_filenames=["salary.txt"]),
    ])
    pipe = FakePipeline({
        "부장 연봉?": _answer("부장은 1억5천 [1].", [_chunk("salary.txt")]),
    })
    report = run_eval(pipe, gold, _resolver, k=5)
    assert not report.passed
    assert report.total_access_violations == 1
    assert "salary.txt" in report.results[0].access_violations


def test_run_eval_denied_answer_is_clean():
    # 권한 없어 근거가 비고 "확인할 수 없습니다" → 위반 없음 + 기대문자열 충족
    gold = GoldSet(items=[
        GoldItem(id="denied", query="부장 연봉?", user_id="u1",
                 forbidden_filenames=["salary.txt"],
                 expected_answer_contains=["확인할 수 없습니다"]),
    ])
    pipe = FakePipeline({"부장 연봉?": _answer("제공된 문서에서 확인할 수 없습니다.", [])})
    report = run_eval(pipe, gold, _resolver, k=5)
    assert report.passed
    assert report.results[0].answer_ok is True


def test_run_eval_with_judge():
    gold = GoldSet(items=[
        GoldItem(id="leave", query="연차 며칠?", user_id="u1",
                 relevant_filenames=["leave.txt"], expected_answer_contains=["15"]),
    ])
    pipe = FakePipeline({"연차 며칠?": _answer("연차는 15일 [1].", [_chunk("leave.txt")])})
    report = run_eval(pipe, gold, _resolver, k=5, judge=FakeJudge())
    assert report.mean_groundedness == 0.9
    assert report.mean_relevance == 0.8
