"""docx 파서: 헤딩 스타일로 섹션을 추적하고, 표는 별도 table 요소로 추출."""

from __future__ import annotations

import docx

from app.schemas.enums import ChunkType, FileFormat

from .base import ParsedElement, ParseResult


def _table_to_markdown(table) -> str:
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    header, *body = rows
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


class DocxParser:
    file_format = FileFormat.DOCX

    def parse(self, path: str) -> ParseResult:
        doc = docx.Document(path)
        elements: list[ParsedElement] = []
        current_section: str | None = None

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name or "").lower() if para.style else ""
            if style.startswith("heading") or style.startswith("title"):
                current_section = text
                elements.append(
                    ParsedElement(text=text, element_type=ChunkType.TEXT,
                                  section_title=current_section)
                )
            else:
                etype = ChunkType.LIST if style.startswith("list") else ChunkType.TEXT
                elements.append(
                    ParsedElement(text=text, element_type=etype,
                                  section_title=current_section)
                )

        for table in doc.tables:
            md = _table_to_markdown(table)
            if md:
                elements.append(
                    ParsedElement(text=md, element_type=ChunkType.TABLE,
                                  section_title=current_section)
                )

        return ParseResult(elements=elements, page_count=None)
