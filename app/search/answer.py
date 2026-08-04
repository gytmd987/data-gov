"""답변 생성 (근거 강제 + 출처 인용).

- 컨텍스트 청크를 [1]..[n] 으로 번호 매겨 프롬프트에 넣고, 근거에서만 답하도록 강제.
- 답변 본문의 [n] 마커를 파싱해 Citation(문서·페이지) 목록을 만든다.
- LLM은 TextLLM Protocol로 주입 → 서비스 없이 테스트 가능.
"""

from __future__ import annotations

import re
from typing import Protocol

from .types import Answer, Citation, RetrievedChunk


class TextLLM(Protocol):
    def complete_text(self, prompt: str) -> str: ...


_SYSTEM = """당신은 회사 인사팀 문서 기반 질의응답 어시스턴트입니다.
반드시 아래 제공된 '근거' 안의 내용만 사용해 답하세요.
근거에 없는 내용은 추측하지 말고 "제공된 문서에서 확인할 수 없습니다"라고 답하세요.
사용한 근거는 문장 끝에 [번호] 형태로 인용하세요(예: 연차는 15일입니다 [1]).

**언어 규칙(중요)**
- 답변은 **한국어로만** 작성하세요.
- 중국어(간체·번체)와 한자를 쓰지 마세요. 한자어는 반드시 한글로 적으세요.
  (예: '年次' ✗ → '연차' ○, '規定' ✗ → '규정' ○, '員工' ✗ → '직원' ○)
- 근거 문서에 한자가 있으면 그 부분만 원문 그대로 인용해도 됩니다.
"""

_CITE_RE = re.compile(r"\[(\d+)\]")

# CJK 통합 한자(+확장A) — 한글 답변에 섞이면 모델이 흘린 것으로 본다.
_HANJA_RE = re.compile(r"[㐀-䶿一-鿿]+")


def has_foreign_script(text: str) -> bool:
    """답변에 한자/중국어가 섞였는가."""
    return bool(_HANJA_RE.search(text or ""))


def strip_foreign_leakage(text: str, source_text: str = "") -> str:
    """근거에 없는 한자/중국어 조각을 지운다.

    중국어권 모델이 한국어 답변 중간에 한자를 흘리는 경우가 있다. 다만 **근거 문서에
    실제로 있는 한자**(사규 원문 등)는 정당한 인용이므로 남긴다.
    """
    if not text:
        return text

    def repl(m: re.Match) -> str:
        token = m.group(0)
        if token in source_text:      # 근거에 있는 표기 → 그대로 둔다
            return token
        return ""

    cleaned = _HANJA_RE.sub(repl, text)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)          # 한자만 있던 괄호 정리
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.!?;:)])", r"\1", cleaned)
    return cleaned.strip()


def build_answer_prompt(query: str, chunks: list[RetrievedChunk]) -> str:
    lines = [_SYSTEM, "\n[근거]"]
    for i, c in enumerate(chunks, start=1):
        loc = []
        if c.title:
            loc.append(c.title)
        if c.page_no is not None:
            loc.append(f"p.{c.page_no}")
        header = f"[{i}]" + (f" ({', '.join(loc)})" if loc else "")
        lines.append(f"{header}\n{c.text}")
    lines.append(f"\n[질문]\n{query}\n\n[답변]")
    return "\n\n".join(lines)


def parse_citations(text: str, chunks: list[RetrievedChunk]) -> list[Citation]:
    citations: list[Citation] = []
    seen: set[int] = set()
    for m in _CITE_RE.finditer(text):
        n = int(m.group(1))
        if n in seen or not (1 <= n <= len(chunks)):
            continue
        seen.add(n)
        c = chunks[n - 1]
        citations.append(Citation(
            marker=n, doc_id=c.doc_id, title=c.title,
            source_filename=c.source_filename,
            page_no=c.page_no, chunk_id=c.chunk_id))
    return sorted(citations, key=lambda c: c.marker)


def generate_answer(
    llm: TextLLM, query: str, chunks: list[RetrievedChunk], retries: int = 1
) -> Answer:
    if not chunks:
        return Answer(text="제공된 문서에서 확인할 수 없습니다.", citations=[], used_chunks=[])
    prompt = build_answer_prompt(query, chunks)
    source_text = "\n".join(c.text or "" for c in chunks)

    text = llm.complete_text(prompt).strip()
    # 한자가 섞이면 한 번 더 요청해 본다(중국어권 모델이 가끔 흘린다).
    for _ in range(max(0, retries)):
        if not has_foreign_script(text) or has_foreign_script(source_text):
            break
        text = llm.complete_text(
            prompt + "\n\n(주의: 앞선 답변에 한자가 섞였습니다. 한국어만 사용해 다시 답하세요.)"
        ).strip()
    text = strip_foreign_leakage(text, source_text)

    citations = parse_citations(text, chunks)
    return Answer(text=text, citations=citations, used_chunks=chunks)
