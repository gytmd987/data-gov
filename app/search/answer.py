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
반드시 아래 제공된 '근거' 안의 내용만 사용해 한국어로 답하세요.
근거에 없는 내용은 추측하지 말고 "제공된 문서에서 확인할 수 없습니다"라고 답하세요.
사용한 근거는 문장 끝에 [번호] 형태로 인용하세요(예: 연차는 15일입니다 [1]).
"""

_CITE_RE = re.compile(r"\[(\d+)\]")


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
    llm: TextLLM, query: str, chunks: list[RetrievedChunk]
) -> Answer:
    if not chunks:
        return Answer(text="제공된 문서에서 확인할 수 없습니다.", citations=[], used_chunks=[])
    prompt = build_answer_prompt(query, chunks)
    text = llm.complete_text(prompt).strip()
    citations = parse_citations(text, chunks)
    return Answer(text=text, citations=citations, used_chunks=chunks)
