"""답변 생성 (근거 강제 + 출처 인용).

- 컨텍스트 청크를 [1]..[n] 으로 번호 매겨 프롬프트에 넣고, 근거에서만 답하도록 강제.
- 답변 본문의 [n] 마커를 파싱해 Citation(문서·페이지) 목록을 만든다.
- LLM은 TextLLM Protocol로 주입 → 서비스 없이 테스트 가능.
"""

from __future__ import annotations

import re
from typing import Optional, Protocol

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


def clip(text: str, limit: int) -> str:
    """근거를 프롬프트에 넣을 만큼만 자른다.

    표(엑셀·워드 표)는 헤더를 지키려고 쪼개지 않으므로 청크 하나가 수만 자가 되기도
    한다. 그대로 넣으면 답변 시작까지(프리필) 그만큼 오래 걸린다. 표는 **앞부분에
    헤더와 대표 행**이 있어 잘라도 대개 판단이 된다. 잘렸다는 사실은 표시한다 —
    모델이 "이게 전부"라고 단정하지 않게.
    """
    if not limit or len(text or "") <= limit:
        return text or ""
    return text[:limit].rstrip() + f"\n…(이하 {len(text) - limit:,}자 생략)"


def build_answer_prompt(query: str, chunks: list[RetrievedChunk],
                        max_chars: Optional[int] = None,
                        total_chars: Optional[int] = None) -> str:
    """근거를 번호 매겨 프롬프트로. 길이는 두 겹으로 묶는다.

    - max_chars  : 근거 **하나당** 상한(표 청크가 수만 자인 경우 대비)
    - total_chars: 프롬프트 **전체** 상한. 이걸 넘으면 뒤쪽 근거부터 뺀다.

    전체 상한이 필요한 이유: vLLM 은 `--max-model-len`(예: 16384) 을 넘는 요청을
    거절한다. 판단 루프가 근거를 14개까지 모으면 상한 없이는 그 선을 넘어 **답변이
    아예 안 나온다.** 뒤쪽은 관련도가 낮은 것들이라 빼도 답에 큰 영향이 없다.
    """
    from app.config import settings
    limit = settings.answer_max_chars if max_chars is None else max_chars
    budget = settings.answer_total_chars if total_chars is None else total_chars

    lines = [_SYSTEM, "\n[근거]"]
    used = 0
    for i, c in enumerate(chunks, start=1):
        loc = []
        if c.title:
            loc.append(c.title)
        if c.page_no is not None:
            loc.append(f"p.{c.page_no}")
        header = f"[{i}]" + (f" ({', '.join(loc)})" if loc else "")
        body = clip(c.text, limit)
        # 첫 근거는 예산을 넘더라도 넣는다 — 근거 없는 답변보다는 낫다
        if budget and i > 1 and used + len(body) > budget:
            break
        used += len(body)
        lines.append(f"{header}\n{body}")
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


NO_EVIDENCE = "제공된 문서에서 확인할 수 없습니다."


def stream_answer(llm, query: str, chunks: list[RetrievedChunk]):
    """답변을 조각으로 흘려보낸다(생성기). 후처리 전 **날것**이다.

    다 만들어 놓고 한 번에 주면 그동안 화면이 멈춰 보인다. 첫 글자부터 내보내려면
    후처리(한자 제거·인용번호 정리)를 뒤로 미뤄야 하므로, 끝난 뒤 `finalize_answer`
    로 정리한 최종본을 화면이 한 번 갈아 끼운다.

    `stream_text` 가 없는 LLM(오프라인 데모·테스트 대역)은 한 번에 내놓는다.
    """
    if not chunks:
        yield NO_EVIDENCE
        return
    prompt = build_answer_prompt(query, chunks)
    streamer = getattr(llm, "stream_text", None)
    if streamer is None:
        yield llm.complete_text(prompt).strip()
        return
    yield from streamer(prompt)


def finalize_answer(raw: str, chunks: list[RetrievedChunk]) -> Answer:
    """흘려보낸 날것 → 한자 정리 + 인용 파싱까지 끝낸 최종 답변."""
    if not chunks:
        return Answer(text=NO_EVIDENCE, citations=[], used_chunks=[])
    source_text = "\n".join(c.text or "" for c in chunks)
    text = strip_foreign_leakage((raw or "").strip(), source_text)
    return Answer(text=text, citations=parse_citations(text, chunks), used_chunks=chunks)


def generate_answer(
    llm: TextLLM, query: str, chunks: list[RetrievedChunk], retries: int = 1
) -> Answer:
    if not chunks:
        return Answer(text=NO_EVIDENCE, citations=[], used_chunks=[])
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
