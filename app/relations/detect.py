"""문서 '연관' 자동 감지 (순수 함수 — DB 비의존, 테스트 가능).

공격적 정책: 아래 신호 중 **하나만** 잡혀도 연관 후보로 본다.
  1. 파일명 어간 일치 — "평가보고서.docx" ↔ "평가보고서_별첨1.xlsx"
  2. 본문 언급 — LLM이 뽑은 references(제목/파일명)가 기존 문서와 매칭
  3. 내용 유사 — 임베딩 유사도 중간대(중복 임계 미만)
사람은 검토 화면에서 틀린 연결만 제거한다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

# 별첨/버전/날짜 등 꼬리표 제거용
_MARKERS = re.compile(r"(별첨|첨부|부록|붙임|annex|appendix|attachment)\s*\d*", re.IGNORECASE)
_TRAILING = re.compile(r"[_\-\s]*v?\d{1,4}([._-]\d{1,2}){0,2}$")
_SEP = re.compile(r"[_\-\s()\[\]]+")

# 내용 유사 '연관' 대역: [floor, dup) — dup 이상은 개정판(별도 처리)
RELATED_FLOOR = 0.60
DUP_THRESHOLD = 0.88


def normalize_stem(filename: str) -> str:
    """파일명에서 확장자·별첨/버전/날짜 꼬리표·구분자를 제거한 정규화 어간."""
    name = Path(filename or "").stem
    name = _MARKERS.sub("", name)
    name = _TRAILING.sub("", name)
    name = _SEP.sub("", name)
    return name.strip().lower()


def detect_related(
    this_doc_id: str,
    this_filename: str,
    mentions: Iterable[str],
    existing: list[dict[str, Any]],           # [{doc_id, filename, title}]
    sim_candidates: Optional[Iterable[dict[str, Any]]] = None,  # [{doc_id, score}]
) -> list[dict[str, Any]]:
    """연관 후보 [{doc_id, reason, confidence}] 반환(doc_id 중복 제거, 최고 신뢰도 유지)."""
    found: dict[str, dict[str, Any]] = {}

    def add(doc_id: str, reason: str, conf: float) -> None:
        if not doc_id or doc_id == this_doc_id:
            return
        cur = found.get(doc_id)
        if cur is None or conf > cur["confidence"]:
            found[doc_id] = {"doc_id": doc_id, "reason": reason, "confidence": round(conf, 3)}

    by_id = {e["doc_id"]: e for e in existing}

    # 1) 파일명 어간 일치
    stem = normalize_stem(this_filename)
    if stem:
        for e in existing:
            if e["doc_id"] != this_doc_id and normalize_stem(e.get("filename") or "") == stem:
                add(e["doc_id"], "파일명", 0.9)

    # 2) 본문 언급(references) → 제목/파일명 어간 매칭
    for m in mentions or []:
        key = _SEP.sub("", (m or "").strip().lower())
        if len(key) < 2:
            continue
        for e in existing:
            title = _SEP.sub("", (e.get("title") or "").lower())
            fstem = normalize_stem(e.get("filename") or "")
            if e["doc_id"] != this_doc_id and key and (
                    key in title or (title and title in key)
                    or key == fstem or (fstem and fstem in key)):
                add(e["doc_id"], "본문 언급", 0.95)

    # 3) 내용 유사 중간대
    for c in sim_candidates or []:
        score = float(c.get("score") or 0.0)
        if RELATED_FLOOR <= score < DUP_THRESHOLD and c["doc_id"] in by_id:
            add(c["doc_id"], "내용 유사", score)

    return sorted(found.values(), key=lambda d: d["confidence"], reverse=True)
