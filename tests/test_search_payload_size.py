"""모델에 보내는 글자 수 — 표 청크가 파이프라인을 느리게 만들던 문제.

표(엑셀·워드 표)는 헤더를 지키려고 **쪼개지 않고 한 청크**로 둔다(`chunking.py`).
그래서 청크 하나가 수만 자가 되기도 한다. 그 전문이 리랭커 후보 24~40개와 답변
프롬프트에 그대로 들어가면 둘 다 그만큼 느려진다.

**보낼 때만 자른다.** 저장된 청크·인용문·다운로드는 원문 그대로여야 한다.
"""

import pytest

from app.clients.reranker import TEIReranker
from app.search.answer import build_answer_prompt, clip
from app.search.types import RetrievedChunk

HUGE = "머리글|급여|부서\n" + "\n".join(f"행{i}|{i * 1000}|인사팀" for i in range(3000))


def _chunk(text, cid="c1", title="급여대장"):
    return RetrievedChunk(chunk_id=cid, text=text, score=1.0,
                          payload={"parent_doc_id": "d1", "title": title})


# ── clip ─────────────────────────────────────────────────────────────────────
def test_short_text_is_untouched():
    assert clip("연차는 15일", 1000) == "연차는 15일"


def test_long_text_is_cut_and_says_so():
    out = clip(HUGE, 500)
    assert len(out) < len(HUGE)
    assert out.startswith("머리글|급여|부서")      # 표는 앞에 헤더가 있다
    assert "생략" in out, "잘렸다는 사실을 모델에게 알려야 한다"


def test_zero_limit_means_no_limit():
    assert clip(HUGE, 0) == HUGE


# ── 답변 프롬프트 ────────────────────────────────────────────────────────────
def test_answer_prompt_bounds_each_chunk():
    prompt = build_answer_prompt("급여 총액은?", [_chunk(HUGE), _chunk(HUGE, "c2")],
                                 max_chars=800)

    assert len(prompt) < 4000, f"프롬프트가 {len(prompt):,}자 — 프리필이 그만큼 느려진다"
    assert prompt.count("생략") == 2


def test_answer_prompt_keeps_titles_and_numbering():
    prompt = build_answer_prompt("질문", [_chunk(HUGE), _chunk("짧은 근거", "c2", "규정")],
                                 max_chars=200)

    assert "[1] (급여대장)" in prompt and "[2] (규정)" in prompt
    assert "짧은 근거" in prompt              # 짧은 건 안 잘린다


def test_prompt_limit_comes_from_settings_by_default(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "answer_max_chars", 300)

    assert len(build_answer_prompt("q", [_chunk(HUGE)])) < 1500


# ── 리랭커 ───────────────────────────────────────────────────────────────────
class _Capture:
    """TEI 로 나가는 본문을 붙잡는 대역."""

    def __init__(self):
        self.sent = []

    def post(self, url, json):
        self.sent.append(json)
        return _Resp([{"index": i, "score": 1.0} for i in range(len(json["texts"]))])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


@pytest.fixture
def capture(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr("app.clients.reranker.httpx.Client", lambda **kw: cap)
    return cap


def test_reranker_truncates_what_it_sends(capture, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "rerank_max_chars", 600)

    TEIReranker(base_url="http://tei").rerank("급여?", [HUGE, HUGE])

    sent = capture.sent[0]["texts"]
    assert all(len(t) <= 600 for t in sent), [len(t) for t in sent]
    assert sent[0].startswith("머리글")


def test_reranker_still_scores_every_candidate_in_order(capture):
    scores = TEIReranker(base_url="http://tei").rerank("q", ["가", HUGE, "다"])
    assert len(scores) == 3


def test_reranker_splits_batches_larger_than_tei_allows(capture, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "tei_max_batch", 10)

    TEIReranker(base_url="http://tei").rerank("q", ["텍스트"] * 25)

    assert [len(c["texts"]) for c in capture.sent] == [10, 10, 5]


def test_truncation_does_not_change_the_stored_chunk(capture, monkeypatch):
    """자르는 건 보낼 때뿐 — 화면에 보이는 인용문은 원문이어야 한다."""
    from app.config import settings
    monkeypatch.setattr(settings, "rerank_max_chars", 100)
    chunk = _chunk(HUGE)

    TEIReranker(base_url="http://tei").rerank("q", [chunk.text])

    assert chunk.text == HUGE


# ── 프롬프트 전체 상한 (vLLM --max-model-len 초과 방지) ──────────────────────
def test_total_budget_drops_the_least_relevant_evidence():
    """상한이 없으면 vLLM 이 요청을 거절해 답이 아예 안 나온다."""
    chunks = [_chunk("가" * 3000, f"c{i}") for i in range(14)]

    prompt = build_answer_prompt("질문", chunks, max_chars=3000, total_chars=9000)

    assert len(prompt) < 12000, f"{len(prompt):,}자 — 모델 입력 한도를 넘는다"
    assert "[1]" in prompt and "[14]" not in prompt   # 뒤쪽(관련도 낮은 것)부터 뺀다


def test_the_first_evidence_survives_even_if_it_alone_exceeds_the_budget():
    """근거를 전부 빼느니 하나라도 넣는 게 낫다."""
    prompt = build_answer_prompt("질문", [_chunk(HUGE)], max_chars=5000, total_chars=100)

    assert "머리글|급여|부서" in prompt


def test_budget_keeps_as_many_as_fit():
    chunks = [_chunk("나" * 1000, f"c{i}") for i in range(10)]

    prompt = build_answer_prompt("질문", chunks, max_chars=1000, total_chars=3500)

    kept = sum(1 for i in range(1, 11) if f"[{i}]\n" in prompt or f"[{i}] " in prompt)
    assert 3 <= kept <= 5, f"{kept}개 들어감 — 예산에 맞게 담기지 않았다"


def test_no_budget_means_everything_goes_in():
    chunks = [_chunk("다" * 1000, f"c{i}") for i in range(6)]
    prompt = build_answer_prompt("질문", chunks, max_chars=1000, total_chars=0)
    assert "[6]" in prompt


# ── 리랭커 끄기 (CPU 폴백 상황의 탈출구) ────────────────────────────────────
def test_disabled_reranker_keeps_the_search_order():
    """리랭커가 CPU 로 돌아 8초씩 먹을 때 — 끄면 융합 순위 그대로 바로 답한다."""
    from app.search.rerank import rerank_chunks

    chunks = [_chunk(f"본문{i}", f"c{i}") for i in range(10)]

    kept = rerank_chunks(None, "질문", chunks, top_k=4)

    assert [c.chunk_id for c in kept] == ["c0", "c1", "c2", "c3"]


def test_disabled_reranker_on_empty_input():
    from app.search.rerank import rerank_chunks
    assert rerank_chunks(None, "질문", [], top_k=4) == []


def test_pipeline_answers_without_a_reranker():
    """리랭커를 꺼도 파이프라인이 끝까지 돌아야 한다."""
    from app.search.access import AccessPolicy, UserContext
    from app.search.pipeline import SearchPipeline

    readable = RetrievedChunk(
        chunk_id="c1", text="연차는 15일이다", score=1.0,
        payload={"parent_doc_id": "d1", "title": "연차규정",
                 "access_groups": ["*"], "status": "active"})

    class _Retriever:
        def retrieve(self, query, policy, top_n=40):
            return [readable]

    class _LLM:
        def complete_text(self, prompt, temperature=0.2):
            return "연차는 15일입니다 [1]"

    pipe = SearchPipeline(retriever=_Retriever(), reranker=None, llm=_LLM())
    user = UserContext(user_id="u", groups=frozenset({"n:1"}))

    answer = pipe.answer("연차?", user)

    assert "15일" in answer.text
