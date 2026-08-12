"""유사 문서 간 관계를 LLM으로 판단(개정판 / 연관 / 무관).

업로드 시 near-dup 후보가 있으면 이 분류를 돌려 검토 화면에 'AI 제안 관계'를 띄운다.
사람은 그 제안을 보고 (새 버전으로 교체 / 연관으로 연결 / 무관) 을 확정한다.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

# 관계 값 → 한글 라벨(화면용)
RELATION_LABELS = {
    "revision": "개정판(새 버전)일 가능성",
    "related": "연관 자료(별첨·참고 등)",
    "unrelated": "무관(별개 문서)",
}

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relation": {"type": "string", "enum": ["revision", "related", "unrelated"]},
        "reason": {"type": "string"},
    },
    "required": ["relation"],
}

_PROMPT = """새로 올라온 문서와 기존 문서의 관계를 판단하세요.

새 문서: 제목「{nt}」
새 문서 요약: {ns}

기존 문서: 제목「{ct}」
기존 문서 요약: {cs}

- revision: 같은 문서의 새 버전/개정판(내용이 갱신됨)
- related: 서로 다른 문서지만 연관(별첨·참고·후속 등)
- unrelated: 무관

relation 과 reason(한 줄 근거)을 채우세요."""


class RelationLLM(Protocol):
    def complete_json(self, prompt: str, schema: dict[str, Any],
                      **tuning: Any) -> dict[str, Any]:
        """tuning: 속도 조절용 선택 인자(kind·max_tokens). 대역은 무시해도 된다."""
        ...


def classify_relation(llm: RelationLLM, new_title: Optional[str], new_summary: Optional[str],
                      cand_title: Optional[str], cand_summary: Optional[str]) -> dict[str, str]:
    """관계 판단 → {relation, reason}. 실패 시 relation='revision'(유사도 높은 후보의 기본 가정)."""
    try:
        res = llm.complete_json(_PROMPT.format(
            nt=new_title or "", ns=new_summary or "",
            ct=cand_title or "", cs=cand_summary or ""), _SCHEMA)
        rel = res.get("relation")
        if rel not in RELATION_LABELS:
            rel = "revision"
        return {"relation": rel, "reason": str(res.get("reason") or "")}
    except Exception:
        return {"relation": "revision", "reason": ""}
