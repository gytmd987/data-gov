"""xlsx 파서 (경로 A: 검색용).

작은 표는 시트 전체를 헤더 보존 마크다운으로 만들어 임베딩한다.
대용량 표(명단·급여 등, 데이터행 > 임계값)는 전체 행을 임베딩하면 잘리고 의미가 없으므로,
**스키마·요약 카드**만 만들어 임베딩한다(검색으로 파일을 찾는 용도). 실제 행 조회는
Tier 2(DuckDB 적재 + text-to-SQL)가 담당한다.
"""

from __future__ import annotations

import openpyxl

from app.datasets.detect import (
    DATA_TABLE_ROW_THRESHOLD,
    build_schema_card,
    is_data_table,
    normalize_headers,
    read_sheet_preview,
)
from app.schemas.enums import ChunkType, FileFormat

from .base import ParsedElement, ParseResult


def _rows_to_markdown(header: list[str], body: list[list[str]]) -> str:
    ncol = len(header)
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in range(ncol)) + " |"]
    for r in body:
        r = (list(r) + [""] * ncol)[:ncol]
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


class XlsxParser:
    file_format = FileFormat.XLSX

    def parse(self, path: str) -> ParseResult:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        elements: list[ParsedElement] = []
        sheet_count = len(wb.worksheets)

        for ws in wb.worksheets:
            header, small_rows, n_rows, samples = read_sheet_preview(
                ws, DATA_TABLE_ROW_THRESHOLD)
            if header is None or n_rows == 0:
                continue
            if is_data_table(n_rows):
                # 대용량 표 → 스키마·요약 카드만(전체 행은 Tier 2 DuckDB 에서)
                text = build_schema_card(ws.title, header, n_rows, samples)
            else:
                cols = normalize_headers(header)
                text = _rows_to_markdown(cols, small_rows)
            elements.append(ParsedElement(text=text, element_type=ChunkType.TABLE,
                                          section_title=ws.title))

        wb.close()
        return ParseResult(elements=elements, page_count=sheet_count)
