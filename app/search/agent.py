"""판단 루프 — 어떻게 찾을지 LLM 이 정하고, 다 찾았다고 판단하면 답한다.

    질문 → (미리 돌려 둔 의미 검색) → LLM 판단 → 도구 실행 → 다시 판단 → … → 답변

**첫 검색은 미리 돌려 둔다.** "연차 며칠?" 같은 흔한 질문은 지금까지의 검색 한 번이면
충분한데, 그걸 LLM 한테 물어보고 나서 검색하면 왕복이 하나 더 늘어 느려진다. 미리
돌려서 근거와 함께 물어보면 대부분 **첫 판단에서 바로 답한다** → 추가 비용은 짧은
판단 호출 하나뿐이다.

안전장치가 셋이다. 어느 쪽으로 틀어져도 답은 나와야 한다.
  - 반복 상한(`max_steps`) — 헛돌면 그때까지 모은 근거로 답한다
  - 도구 실행 실패는 오류 문자열로 LLM 에게 돌려준다(예외로 죽지 않는다)
  - 판단 자체가 실패하면 미리 돌려 둔 검색 결과로 답한다 = 지금까지의 동작
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from app.search.tools import ToolBox, ToolResult
from app.search.types import RetrievedChunk

MAX_STEPS = 8
MAX_EVIDENCE = 24          # 최종 답변 프롬프트에 넣을 근거 조각 상한
_SCRATCH_CHARS = 6000      # 지금까지의 진행 기록 상한(넘으면 앞을 버린다)

# 도구 인자를 **평평한 오브젝트 하나**로 받는다. 도구마다 모양이 다른 스키마(oneOf)는
# 로컬 모델이 자주 어긋나므로, 전부 선택 항목으로 두고 코드에서 필요한 것만 읽는다.
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "이유": {"type": "string"},
        "도구": {"type": "string",
                 "enum": ["문서_세기", "문서_목록", "내용_검색", "문서_읽기", "답변하기"]},
        "검색어": {"type": "string"},
        "문서종류": {"type": "array", "items": {"type": "string"}},
        "문서id": {"type": "string"},
        "개수": {"type": "integer"},
    },
    "required": ["이유", "도구"],
}

_GUIDE = """당신은 사내 문서를 찾아 주는 조사원입니다.
질문에 답하려면 어떤 자료가 필요한지 스스로 판단하고, 아래 도구를 써서 모으세요.

[도구]
- 내용_검색  : 의미가 비슷한 본문 조각을 찾습니다. 인자 `검색어`(필수), `문서종류`, `개수`
               → 내용을 묻는 질문("연차 며칠?")에 씁니다.
- 문서_목록  : 조건에 맞는 문서를 **목록으로** 뽑습니다. 인자 `문서종류`, `검색어`, `개수`
               → "무슨 문서가 있나", "규정 전부" 처럼 **빠짐없이 훑어야 할 때** 씁니다.
                 내용_검색은 상위 몇 개만 주므로 전수 확인에는 쓸 수 없습니다.
- 문서_세기  : 조건에 맞는 문서가 몇 건인지 셉니다. 인자 `문서종류`, `검색어`
               → 전수로 훑을지 범위를 좁힐지 **먼저 규모를 재 볼 때** 씁니다.
- 문서_읽기  : 특정 문서를 자세히 읽습니다. 인자 `문서id`(필수)
               → 목록에서 고른 문서를 하나씩 확인할 때 씁니다.
- 답변하기   : 모은 자료로 충분하면 이걸 고르세요.

[규칙]
- **자료가 충분하면 곧바로 `답변하기` 를 고르세요.** 이미 답할 수 있는데 도구를 더
  부르면 사용자만 기다립니다.
- 빠짐없이 찾아야 하는 질문이면 `문서_세기` → `문서_목록` → 필요한 것만 `문서_읽기`
  순으로 가세요. 세어 봐서 너무 많으면 범위를 좁히거나, 좁힐 수 없다고 답하세요.
- 같은 도구를 같은 인자로 두 번 부르지 마세요. 결과가 같습니다.
- `문서id` 는 **앞의 결과에 나온 값만** 쓰세요. 지어내지 마세요.
- `이유` 에는 왜 그 도구를 고르는지 한 문장으로 적으세요."""


@dataclass
class AgentStep:
    tool: str
    reason: str
    note: str


@dataclass
class AgentResult:
    evidence: list[RetrievedChunk] = field(default_factory=list)
    steps: list[AgentStep] = field(default_factory=list)
    transcript: str = ""
    gave_up: bool = False        # 상한에 걸려 중단(모은 근거로는 답한다)
    planned: bool = False        # 판단 루프가 실제로 동작했나(False 면 기존 경로)


def run_agent(llm, question: str, tools: ToolBox, *,
              prefetch: Optional[ToolResult] = None,
              max_steps: int = MAX_STEPS):
    """질문 → 근거. **생성기다**: 진행 상황(`AgentStep`)을 한 걸음마다 내보내고,
    끝나면 `AgentResult` 를 반환값으로 돌려준다(`yield from` 으로 받는다).

    한 걸음씩 내보내야 화면이 "규정 47건 확인 → 12건 추림 → 5/12 검토 중" 을
    실시간으로 보여 줄 수 있다. 다 끝나고 한꺼번에 주면 멈춘 것처럼 보인다.

    답변 문장은 만들지 않는다 — 그건 스트리밍으로 따로 생성한다.
    """
    result = AgentResult()
    scratch: list[str] = []
    evidence: dict[str, RetrievedChunk] = {}

    def remember(res: ToolResult) -> None:
        for c in res.chunks:
            evidence.setdefault(c.chunk_id, c)

    if prefetch is not None:
        remember(prefetch)
        scratch.append(f"[내용_검색] (질문으로 미리 검색해 둔 결과)\n{prefetch.text}")

    runner = {
        "문서_세기": tools.count_documents,
        "문서_목록": tools.list_documents,
        "내용_검색": tools.search_chunks,
        "문서_읽기": tools.read_document,
    }
    seen: set[str] = set()

    for _ in range(max(1, max_steps)):
        try:
            decision = llm.complete_json(_prompt(question, scratch), DECISION_SCHEMA)
        except Exception:
            break                                  # 판단 실패 → 모은 것으로 답한다
        result.planned = True

        tool = str(decision.get("도구") or "").strip()
        reason = str(decision.get("이유") or "").strip()
        if tool == "답변하기" or tool not in runner:
            break

        key = tool + json.dumps(decision, ensure_ascii=False, sort_keys=True)
        if key in seen:                            # 같은 호출 반복 → 진전 없음
            break
        seen.add(key)

        try:
            res = runner[tool](**decision)
        except Exception as e:                     # 도구가 죽어도 루프는 계속된다
            res = ToolResult(text=f"도구 실행 실패: {type(e).__name__}",
                             note="실행 실패")
        remember(res)
        scratch.append(f"[{tool}] {reason}\n{res.text}")

        step = AgentStep(tool=tool, reason=reason, note=res.note)
        result.steps.append(step)
        yield step
    else:
        result.gave_up = True

    result.evidence = list(evidence.values())[:MAX_EVIDENCE]
    result.transcript = "\n\n".join(scratch)
    return result


def _prompt(question: str, scratch: list[str]) -> str:
    body = "\n\n".join(scratch)
    if len(body) > _SCRATCH_CHARS:                 # 오래된 것부터 버린다
        body = "…(앞부분 생략)…\n" + body[-_SCRATCH_CHARS:]
    parts = [_GUIDE, f"[질문]\n{question}"]
    if body:
        parts.append(f"[지금까지 모은 자료]\n{body}")
    else:
        parts.append("[지금까지 모은 자료]\n(없음)")
    parts.append("[다음에 할 일을 JSON 으로]")
    return "\n\n".join(parts)
