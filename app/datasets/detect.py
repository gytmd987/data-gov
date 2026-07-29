"""표 데이터 감지 + 스키마 카드 구성(Tier 1) 공용 상수·헬퍼.

xlsx_parser(임베딩용 카드)와 loader(DuckDB 적재)가 같은 임계값을 공유한다.
"""

from __future__ import annotations

from typing import Optional

# 데이터행 수가 이 값을 넘으면 '대용량 표(명단류)' → 전체 임베딩 대신 카드만.
DATA_TABLE_ROW_THRESHOLD = 30
SAMPLE_ROWS = 5              # 카드/프롬프트에 넣을 미리보기 행 수
_CARD_MAX_CHARS = 1500      # 카드 텍스트 상한(임베딩 한 청크 유지)


def is_data_table(n_data_rows: int) -> bool:
    return n_data_rows > DATA_TABLE_ROW_THRESHOLD


def normalize_headers(header: list[str]) -> list[str]:
    """빈/중복 헤더 정리 → 유효한 컬럼명 목록(원본 최대한 유지, 충돌만 접미)."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, h in enumerate(header):
        name = (h or "").strip() or f"col{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


def _rows_md(header: list[str], rows: list[list[str]]) -> str:
    ncol = len(header)
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in range(ncol)) + " |"]
    for r in rows:
        r = (list(r) + [""] * ncol)[:ncol]
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


def build_schema_card(sheet: str, header: list[str], n_data_rows: int,
                      samples: list[list[str]]) -> str:
    """대용량 표의 스키마·요약 카드(이것만 임베딩된다)."""
    cols = normalize_headers(header)
    card = (
        f"[데이터 표] 시트: {sheet}\n"
        f"행: {n_data_rows}행 · 열: {len(cols)}열\n"
        f"열 목록: {', '.join(cols)}\n"
        f"샘플(상위 {min(SAMPLE_ROWS, len(samples))}행):\n"
        f"{_rows_md(cols, samples[:SAMPLE_ROWS])}\n"
        "※ 전체 데이터는 원본 파일에서 확인/다운로드하세요(대용량 표는 요약만 색인됩니다)."
    )
    return card[:_CARD_MAX_CHARS]


def read_sheet_preview(ws, threshold: int = DATA_TABLE_ROW_THRESHOLD
                       ) -> tuple[Optional[list[str]], list[list[str]], int, list[list[str]]]:
    """시트를 스트리밍해 (header, small_rows, n_data_rows, samples) 반환.

    - small_rows: 임계값 이내면 전체 데이터행(소형 표 마크다운용), 초과하면 절단(무의미).
    - samples: 앞 SAMPLE_ROWS 행.
    메모리를 위해 임계값+여유까지만 행을 보관한다.
    """
    header: Optional[list[str]] = None
    small_rows: list[list[str]] = []
    samples: list[list[str]] = []
    n = 0
    keep = threshold + 1
    for row in ws.iter_rows(values_only=True):
        if row is None:
            continue
        cells = ["" if c is None else str(c).strip() for c in row]
        if not any(cells):
            continue
        if header is None:
            header = cells
            continue
        n += 1
        if len(samples) < SAMPLE_ROWS:
            samples.append(cells)
        if len(small_rows) < keep:
            small_rows.append(cells)
    return header, small_rows, n, samples
