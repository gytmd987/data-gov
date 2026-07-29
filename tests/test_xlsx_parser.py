"""xlsx 파서: 대용량 표는 스키마 카드, 소형 표는 전체 마크다운."""

import openpyxl
import pytest

from app.datasets.detect import DATA_TABLE_ROW_THRESHOLD
from app.ingestion.parsers.xlsx_parser import XlsxParser
from app.schemas.enums import ChunkType


def _make_xlsx(path, sheets: dict[str, tuple[list, int]]):
    """sheets = {시트명: (헤더, 데이터행수)}."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, (header, nrows) in sheets.items():
        ws = wb.create_sheet(title=name)
        ws.append(header)
        for i in range(nrows):
            ws.append([f"{col}{i}" for col in header])
    wb.save(path)


def test_large_sheet_becomes_schema_card(tmp_path):
    p = tmp_path / "roster.xlsx"
    _make_xlsx(p, {"명단": (["사번", "이름", "부서"], DATA_TABLE_ROW_THRESHOLD + 50)})
    res = XlsxParser().parse(str(p))
    assert len(res.elements) == 1
    el = res.elements[0]
    assert el.element_type == ChunkType.TABLE
    assert "[데이터 표]" in el.text
    assert "열 목록: 사번, 이름, 부서" in el.text
    assert f"행: {DATA_TABLE_ROW_THRESHOLD + 50}행" in el.text
    # 전체 행이 아니라 요약만 → 뒷쪽 데이터행은 카드에 없다
    assert f"이름{DATA_TABLE_ROW_THRESHOLD + 40}" not in el.text
    assert len(el.text) <= 1600


def test_small_sheet_becomes_full_table(tmp_path):
    p = tmp_path / "small.xlsx"
    _make_xlsx(p, {"표": (["a", "b"], 3)})
    res = XlsxParser().parse(str(p))
    assert len(res.elements) == 1
    txt = res.elements[0].text
    assert "[데이터 표]" not in txt
    assert "| a | b |" in txt and "a0" in txt and "a2" in txt   # 전체 행 포함


def test_mixed_workbook(tmp_path):
    p = tmp_path / "mixed.xlsx"
    _make_xlsx(p, {"작음": (["x"], 2),
                   "큼": (["y", "z"], DATA_TABLE_ROW_THRESHOLD + 5)})
    res = XlsxParser().parse(str(p))
    by_sheet = {e.section_title: e.text for e in res.elements}
    assert "[데이터 표]" not in by_sheet["작음"]
    assert "[데이터 표]" in by_sheet["큼"]
    assert res.page_count == 2
