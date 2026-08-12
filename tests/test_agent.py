"""판단 루프 — 도구 선택, 안전장치, 권한."""

from datetime import date

import pytest

from app.search.access import AccessPolicy, UserContext, Visibility
from app.search.agent import AgentStep, run_agent
from app.search.tools import ToolBox, ToolResult
from app.search.types import RetrievedChunk


class ScriptedLLM:
    """정해 둔 순서대로 결정을 내놓는 대역. 다 쓰면 '답변하기'."""

    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.prompts: list[str] = []

    def complete_json(self, prompt, schema, **_):
        self.prompts.append(prompt)
        if not self.decisions:
            return {"이유": "충분함", "도구": "답변하기"}
        return self.decisions.pop(0)


class FakeBox:
    """도구 호출을 기록만 하는 ToolBox 대역."""

    def __init__(self, **results):
        self.calls: list[tuple[str, dict]] = []
        self.results = results

    def _run(self, name, cond):
        self.calls.append((name, cond))
        return self.results.get(name, ToolResult(text=f"{name} 결과", note=name))

    def count_documents(self, **c): return self._run("문서_세기", c)
    def list_documents(self, **c): return self._run("문서_목록", c)
    def search_chunks(self, **c): return self._run("내용_검색", c)
    def read_document(self, **c): return self._run("문서_읽기", c)


def _drain(gen):
    """생성기를 끝까지 돌려 (내보낸 걸음들, 반환값) 을 얻는다."""
    steps = []
    while True:
        try:
            steps.append(next(gen))
        except StopIteration as stop:
            return steps, stop.value


def _chunk(cid, text="본문", **payload):
    payload.setdefault("parent_doc_id", cid)
    payload.setdefault("title", cid)
    return RetrievedChunk(chunk_id=cid, text=text, score=1.0, payload=payload)


# ── 흔한 질문: 미리 찾아 둔 것으로 바로 답한다 ───────────────────────────────
def test_answers_immediately_when_prefetch_is_enough():
    llm = ScriptedLLM({"이유": "이미 근거가 있음", "도구": "답변하기"})
    box = FakeBox()
    seed = ToolResult(text="연차는 15일", chunks=[_chunk("c1", "연차는 15일")])

    steps, result = _drain(run_agent(llm, "연차 며칠?", box, prefetch=seed))

    assert box.calls == [], "바로 답할 수 있는데 도구를 불렀다"
    assert steps == []
    assert [c.chunk_id for c in result.evidence] == ["c1"]
    assert result.planned and not result.gave_up


def test_prefetched_evidence_is_shown_to_the_model():
    llm = ScriptedLLM()
    seed = ToolResult(text="연차는 15일이다", chunks=[_chunk("c1")])

    _drain(run_agent(llm, "연차?", FakeBox(), prefetch=seed))

    assert "연차는 15일이다" in llm.prompts[0], "미리 찾은 근거가 프롬프트에 없다"


# ── 훑어야 하는 질문: 여러 도구를 순서대로 ───────────────────────────────────
def test_sweeps_with_multiple_tools_and_reports_each_step():
    llm = ScriptedLLM(
        {"이유": "규모 확인", "도구": "문서_세기", "문서종류": ["regulation"]},
        {"이유": "목록 확보", "도구": "문서_목록", "문서종류": ["regulation"]},
        {"이유": "본문 확인", "도구": "문서_읽기", "문서id": "d1"},
        {"이유": "충분함", "도구": "답변하기"},
    )
    box = FakeBox(
        문서_세기=ToolResult(text="47건", note="문서 47건 확인"),
        문서_목록=ToolResult(text="목록", chunks=[_chunk("d1::doc")], note="목록 12건"),
        문서_읽기=ToolResult(text="본문", chunks=[_chunk("d1::doc")], note="'출장규정' 읽음"),
    )

    steps, result = _drain(run_agent(llm, "법 개정 영향 규정", box, prefetch=None))

    assert [c[0] for c in box.calls] == ["문서_세기", "문서_목록", "문서_읽기"]
    assert [s.note for s in steps] == ["문서 47건 확인", "목록 12건", "'출장규정' 읽음"]
    assert all(isinstance(s, AgentStep) for s in steps)
    assert not result.gave_up


def test_steps_stream_out_one_at_a_time():
    """다 끝난 뒤가 아니라 **한 걸음마다** 나와야 화면이 진행 상황을 보여 준다."""
    llm = ScriptedLLM(
        {"이유": "1", "도구": "문서_세기"},
        {"이유": "2", "도구": "문서_목록"},
        {"이유": "3", "도구": "답변하기"},
    )
    box = FakeBox()
    gen = run_agent(llm, "질문", box, prefetch=None)

    next(gen)                                  # 첫 걸음
    assert len(box.calls) == 1, "첫 걸음을 내보내기 전에 도구를 다 돌렸다"
    next(gen)
    assert len(box.calls) == 2


# ── 안전장치 ─────────────────────────────────────────────────────────────────
def test_stops_when_the_same_call_repeats():
    """같은 호출을 반복하면 진전이 없다 — 상한까지 헛돌지 않고 멈춘다."""
    same = {"이유": "또", "도구": "내용_검색", "검색어": "연차"}
    llm = ScriptedLLM(same, same, same, same)
    box = FakeBox()

    _steps, result = _drain(run_agent(llm, "연차?", box, prefetch=None))

    assert len(box.calls) == 1, f"같은 호출을 {len(box.calls)}번 반복했다"
    assert not result.gave_up


def test_gives_up_after_max_steps_but_still_returns_evidence():
    llm = ScriptedLLM(*[{"이유": str(i), "도구": "내용_검색", "검색어": f"q{i}"}
                        for i in range(10)])
    box = FakeBox(내용_검색=ToolResult(text="결과", chunks=[_chunk("c1")]))

    _steps, result = _drain(run_agent(llm, "질문", box, prefetch=None, max_steps=3))

    assert len(box.calls) == 3, "반복 상한을 안 지켰다"
    assert result.gave_up
    assert result.evidence, "상한에 걸려도 모은 근거는 돌려줘야 한다"


def test_tool_failure_does_not_stop_the_loop():
    class Boom(FakeBox):
        def search_chunks(self, **c):
            raise RuntimeError("검색 서버 죽음")

    llm = ScriptedLLM({"이유": "검색", "도구": "내용_검색", "검색어": "연차"},
                      {"이유": "그럼 목록", "도구": "문서_목록"},
                      {"이유": "충분", "도구": "답변하기"})
    box = Boom()

    steps, result = _drain(run_agent(llm, "연차?", box, prefetch=None))

    assert [s.note for s in steps] == ["실행 실패", "문서_목록"]
    assert not result.gave_up


def test_planner_failure_falls_back_to_prefetched_evidence():
    class Broken:
        def complete_json(self, prompt, schema, **_):
            raise RuntimeError("모델 죽음")

    seed = ToolResult(text="연차 15일", chunks=[_chunk("c1")])
    _steps, result = _drain(run_agent(Broken(), "연차?", FakeBox(), prefetch=seed))

    assert not result.planned, "판단이 실패했는데 성공으로 기록됐다"
    assert [c.chunk_id for c in result.evidence] == ["c1"], "기존 검색 결과로 안 떨어졌다"


def test_unknown_tool_name_ends_the_loop():
    llm = ScriptedLLM({"이유": "??", "도구": "문서_삭제"})
    box = FakeBox()

    _steps, result = _drain(run_agent(llm, "질문", box, prefetch=None))

    assert box.calls == [], "모르는 도구 이름으로 뭔가 실행됐다"
    assert not result.gave_up


# ── 권한: 도구가 사용자 범위를 벗어나면 안 된다 ─────────────────────────────
def _box(session, tokens, retrieve=None):
    user = UserContext(user_id="u@x.com", groups=frozenset(tokens))
    return ToolBox(session=session,
                   policy=AccessPolicy.for_user(user, today=date(2026, 1, 1)),
                   visibility=Visibility(read_tokens=frozenset(tokens)),
                   retrieve=retrieve or (lambda q, p, n: []),
                   today=date(2026, 1, 1))


def test_search_tool_never_returns_documents_the_policy_rejects():
    """검색기가 실수로 흘려도 도구 단계에서 다시 걸러진다."""
    leaked = _chunk("secret", "대외비", access_groups=["n:99"], status="active")
    allowed = _chunk("mine", "공개", access_groups=["n:1"], status="active")
    box = _box(None, {"n:1"}, retrieve=lambda q, p, n: [c for c in (leaked, allowed)
                                                       if p.allows(c.payload)])

    res = box.search_chunks(검색어="연차")

    assert [c.chunk_id for c in res.chunks] == ["mine"]
    assert "대외비" not in res.text


def test_llm_supplied_conditions_cannot_widen_the_permission_filter():
    """LLM 이 무슨 조건을 주든 `visible_to` 는 코드가 붙인 값이어야 한다."""
    box = _box(None, {"n:1"})

    filters = box._filters({"문서종류": ["regulation"], "검색어": "연차",
                            "visible_to": "관리자인척", "access_groups": ["*"]})

    assert filters["visible_to"] is box.visibility
    assert "access_groups" not in filters
    assert filters["doc_type"] == "regulation"


def test_unknown_doc_type_is_dropped_rather_than_passed_through():
    box = _box(None, {"n:1"})
    assert "doc_type" not in box._filters({"문서종류": ["아무거나"]})
