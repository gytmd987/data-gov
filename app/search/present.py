"""답변 출처 표현 유틸 (UI 공용).

같은 파일의 여러 인용 청크를 문서 단위로 묶어, 화면에 "출처 1개 = 파일 1개"로 보여준다.
Django 채팅과 Streamlit 질문하기가 함께 사용한다.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from .types import Answer


def _is_past(payload: dict[str, Any], today: date) -> bool:
    """이 문서가 현행이 아닌 과거(만료·대체) 자료인지. 유효·보관은 현행."""
    if payload.get("status") not in (None, "active", "archived"):
        return True
    expiry = payload.get("expiry_date")
    if expiry:
        try:
            if date.fromisoformat(expiry[:10]) < today:
                return True
        except ValueError:
            pass
    if payload.get("superseded_by"):
        return True
    return False


def group_sources(answer: Answer, today: Optional[date] = None) -> list[dict[str, Any]]:
    """인용을 문서(파일) 단위로 그룹핑.

    반환: [{label, doc_id, markers: [1,2,...], passages: [{marker, page, text}], is_past}]
    is_past=True 면 과거/만료 자료 → UI에서 '과거 자료' 배지로 구분 표시.
    """
    today = today or date.today()
    used_by_id = {c.chunk_id: c for c in answer.used_chunks}
    groups: dict[str, dict[str, Any]] = {}
    for cit in answer.citations:
        key = cit.doc_id or cit.label
        g = groups.setdefault(key, {"label": cit.label, "doc_id": cit.doc_id,
                                    "markers": [], "passages": [], "is_past": False})
        if cit.marker not in g["markers"]:
            g["markers"].append(cit.marker)
        uc = used_by_id.get(cit.chunk_id)
        if uc is not None:
            if _is_past(uc.payload, today):
                g["is_past"] = True
            if uc.text:
                g["passages"].append({"marker": cit.marker, "page": cit.page_no,
                                      "text": uc.text})
    out = list(groups.values())
    for i, g in enumerate(out, start=1):
        g["markers"].sort()
        g["passages"].sort(key=lambda p: p["marker"])
        g["index"] = i          # 화면에 보이는 출처 번호
    return out


def renumber_citations(text: str, sources: list[dict[str, Any]]) -> str:
    """답변 본문의 `[청크번호]` 를 **화면의 출처 번호**로 바꾼다.

    본문은 청크 단위(`[2]`)로 인용하는데 출처는 문서 단위로 묶여서, 그대로 두면
    답변의 [2] 와 '출처 1' 이 어긋나 읽는 사람이 헷갈린다. 같은 문서를 가리키는
    마커는 하나의 출처 번호로 합치고, 어떤 출처에도 안 걸린 마커는 지운다.
    """
    import re

    mapping: dict[int, int] = {}
    for g in sources:
        for m in g.get("markers", []):
            mapping[m] = g["index"]

    def sub(match: "re.Match") -> str:
        n = int(match.group(1))
        return f"[{mapping[n]}]" if n in mapping else ""

    out = re.sub(r"\[(\d+)\]", sub, text or "")
    out = re.sub(r"(\[\d+\])(?:\s*\1)+", r"\1", out)      # 같은 번호 연속 중복 제거
    out = re.sub(r"[ \t]+(\[\d+\])", r"\1", out)          # '…입니다 [1].' → '…입니다[1].'
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r"\s+([,.!?;:])", r"\1", out).strip()
