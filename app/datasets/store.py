"""DuckDB 구조화 저장소(Tier 2).

표 데이터를 파일 단위 테이블로 적재하고, 읽기 전용으로 SQL 질의한다.
DuckDB 는 임베디드(서버 불필요)라 폐쇄망 온프레미스에 적합하다.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from app.config import settings

_IDENT_OK = re.compile(r'^[A-Za-z0-9_]+$')


def quote_ident(name: str) -> str:
    """DuckDB 식별자 안전 인용(한글 컬럼·공백 허용). 내부 큰따옴표는 이스케이프."""
    return '"' + str(name).replace('"', '""') + '"'


def safe_table_name(doc_id: str, sheet: str) -> str:
    """doc_id + 시트명 → 물리 테이블명(영숫자/언더스코어)."""
    slug = re.sub(r'\W+', '_', f"{doc_id}_{sheet}").strip('_').lower()
    return f"ds_{slug}"[:120]


def infer_types(header: list[str], rows: list[list[str]]) -> list[str]:
    """샘플 값으로 컬럼 타입 추론: 전부 숫자→DOUBLE, 전부 날짜→DATE, 그 외 VARCHAR."""
    types: list[str] = []
    for ci in range(len(header)):
        vals = [(r[ci] if ci < len(r) else "") for r in rows]
        nonempty = [v for v in vals if v not in ("", None)]
        if not nonempty:
            types.append("VARCHAR")
            continue
        if all(_looks_number(v) for v in nonempty):
            types.append("DOUBLE")
        elif all(_looks_date(v) for v in nonempty):
            types.append("DATE")
        else:
            types.append("VARCHAR")
    return types


def _looks_number(v: str) -> bool:
    try:
        float(str(v).replace(",", ""))
        return True
    except ValueError:
        return False


def _looks_date(v: str) -> bool:
    return bool(re.match(r'^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$', str(v).strip()))


def _coerce(value: str, coltype: str):
    if value in ("", None):
        return None
    if coltype == "DOUBLE":
        try:
            return float(str(value).replace(",", ""))
        except ValueError:
            return None
    if coltype == "DATE":
        s = re.sub(r'[/.]', "-", str(value).strip())
        return s or None
    return str(value)


class DuckDBStore:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or settings.duckdb_path
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self, read_only: bool = False):
        import duckdb
        # 파일이 없으면 read_only 연결이 실패하므로 최초 1회 생성 보장
        if read_only and not os.path.exists(self.path):
            duckdb.connect(self.path).close()
        return duckdb.connect(self.path, read_only=read_only)

    def create_from_rows(self, table: str, columns: list[str], types: list[str],
                         rows: Iterable[list[str]], batch: int = 5000) -> int:
        """테이블을 (재)생성하고 행을 배치 삽입. 반환: 삽입 행 수."""
        cols = [quote_ident(c) for c in columns]
        con = self._connect()
        try:
            con.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")
            coldefs = ", ".join(f"{c} {t}" for c, t in zip(cols, types))
            con.execute(f"CREATE TABLE {quote_ident(table)} ({coldefs})")
            placeholders = ", ".join("?" for _ in columns)
            insert = f"INSERT INTO {quote_ident(table)} VALUES ({placeholders})"
            buf: list[list] = []
            n = 0
            for r in rows:
                r = (list(r) + [""] * len(columns))[:len(columns)]
                buf.append([_coerce(v, t) for v, t in zip(r, types)])
                if len(buf) >= batch:
                    con.executemany(insert, buf)
                    n += len(buf)
                    buf = []
            if buf:
                con.executemany(insert, buf)
                n += len(buf)
            return n
        finally:
            con.close()

    def query(self, sql: str) -> tuple[list[str], list[list[Any]]]:
        """읽기 전용 SQL 실행 → (컬럼명, 행들)."""
        con = self._connect(read_only=True)
        try:
            cur = con.execute(sql)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [list(r) for r in cur.fetchall()]
            return cols, rows
        finally:
            con.close()

    def drop(self, table: str) -> None:
        con = self._connect()
        try:
            con.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")
        finally:
            con.close()
