"""xlsx 파서 (경로 A: 검색용).

각 시트를 헤더 보존 마크다운 표로 직렬화해 table 요소로 만든다. section_title = 시트명.
집계·정형 질의를 위한 경로 B(DuckDB 적재)는 별도 모듈에서 처리한다.
"""

from __future__ import annotations

import openpyxl

from app.schemas.enums import ChunkType, FileFormat

from .base import ParsedElement, ParseResult


def _rows_to_markdown(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header, *body = rows
    ncol = len(header)
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in range(ncol)) + " |"]
    for r in body:
        r = (r + [""] * ncol)[:ncol]
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


class XlsxParser:
    file_format = FileFormat.XLSX

    def parse(self, path: str) -> ParseResult:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        elements: list[ParsedElement] = []
        sheet_count = len(wb.worksheets)

        for ws in wb.worksheets:
            rows: list[list[str]] = []
            for row in ws.iter_rows(values_only=True):
                if row is None:
                    continue
                cells = ["" if c is None else str(c).strip() for c in row]
                if any(cells):
                    rows.append(cells)
            if not rows:
                continue
            md = _rows_to_markdown(rows)
            elements.append(
                ParsedElement(text=md, element_type=ChunkType.TABLE,
                              section_title=ws.title)
            )

        wb.close()
        return ParseResult(elements=elements, page_count=sheet_count)
