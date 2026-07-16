"""txt 파서: 빈 줄 기준 문단 분할."""

from __future__ import annotations

from pathlib import Path

from app.schemas.enums import ChunkType, FileFormat

from .base import ParsedElement, ParseResult


class TextParser:
    file_format = FileFormat.TXT

    def parse(self, path: str) -> ParseResult:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        elements: list[ParsedElement] = []
        for block in raw.split("\n\n"):
            block = block.strip()
            if block:
                elements.append(
                    ParsedElement(text=block, element_type=ChunkType.TEXT)
                )
        return ParseResult(elements=elements, page_count=None)
