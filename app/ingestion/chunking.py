"""구조 인지 청킹.

원칙:
  - 표(table)/이미지 캡션은 **쪼개지 않고** 한 청크로 유지(헤더 보존).
  - 텍스트는 섹션 경계를 우선하고, 목표 길이를 넘으면 문장/공백 경계로 분할 + 오버랩.
  - 각 청크에 section_title/page_no/chunk_type 를 부여한다(인용·필터용).

길이는 한국어 특성을 고려해 문자 수 기준의 근사치를 쓴다(토크나이저 비의존).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.enums import ChunkType
from app.schemas.metadata import ChunkMetadata

from .parsers.base import ParsedElement

# 문자 수 기준 근사(한국어). 필요 시 토크나이저 기반으로 교체 가능.
DEFAULT_TARGET_CHARS = 1200
DEFAULT_OVERLAP_CHARS = 150


@dataclass
class Chunk:
    meta: ChunkMetadata
    text: str


def _split_long_text(text: str, target: int, overlap: int) -> list[str]:
    if len(text) <= target:
        return [text]
    pieces: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + target, n)
        # 문장/공백 경계로 뒤로 물러나 자연스럽게 자른다
        if end < n:
            window = text.rfind("\n", start, end)
            if window <= start:
                window = text.rfind(". ", start, end)
            if window <= start:
                window = text.rfind(" ", start, end)
            if window > start:
                end = window
        pieces.append(text[start:end].strip())
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return [p for p in pieces if p]


def chunk_elements(
    parent_doc_id: str,
    elements: list[ParsedElement],
    target_chars: int = DEFAULT_TARGET_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    seq = 0

    for el in elements:
        # 표/리스트/이미지 캡션은 통째로 유지
        if el.element_type in (ChunkType.TABLE, ChunkType.IMAGE_CAPTION, ChunkType.LIST):
            texts = [el.text]
        else:
            texts = _split_long_text(el.text, target_chars, overlap_chars)

        for t in texts:
            meta = ChunkMetadata(
                chunk_id=f"{parent_doc_id}::{seq}",
                parent_doc_id=parent_doc_id,
                chunk_type=el.element_type,
                section_title=el.section_title,
                page_no=el.page_no,
            )
            chunks.append(Chunk(meta=meta, text=t))
            seq += 1

    return chunks
