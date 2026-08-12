"""LLM-as-judge: 답변의 groundedness / answer relevance 채점 (로컬 LLM, 외부 API 없음).

JudgeLLM은 enrichment.LLMClient 와 동일한 complete_json 프로토콜을 재사용 → 같은 vLLM 클라이언트
사용 가능, 테스트는 fake 주입.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel


class JudgeLLM(Protocol):
    def complete_json(self, prompt: str, schema: dict[str, Any],
                      **tuning: Any) -> dict[str, Any]:
        """tuning: 속도 조절용 선택 인자(kind·max_tokens). 대역은 무시해도 된다."""
        ...


class JudgeScore(BaseModel):
    groundedness: float   # 근거 일치도(0~1): 답변이 제공 근거에서만 나왔는가
    relevance: float      # 질문 관련도(0~1)


_SCHEMA = {
    "type": "object",
    "properties": {
        "groundedness": {"type": "number", "minimum": 0, "maximum": 1},
        "relevance": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["groundedness", "relevance"],
}

_PROMPT = """당신은 RAG 답변을 평가하는 채점자입니다. 아래를 읽고 0~1 점수를 매기세요.
- groundedness: 답변 내용이 '근거'에서만 뒷받침되는가(추측/환각이 없는가).
- relevance: 답변이 '질문'에 얼마나 관련 있는가.

[질문]
{query}

[근거]
{context}

[답변]
{answer}
"""


def judge_answer(
    judge: JudgeLLM, query: str, answer: str, context_texts: list[str]
) -> JudgeScore:
    context = "\n---\n".join(context_texts) if context_texts else "(근거 없음)"
    prompt = _PROMPT.format(query=query, context=context[:6000], answer=answer)
    result = judge.complete_json(prompt, _SCHEMA)
    return JudgeScore(
        groundedness=float(result.get("groundedness", 0.0)),
        relevance=float(result.get("relevance", 0.0)),
    )
