"""Tier 2: DuckDB 적재/질의, 타입추론, text-to-SQL 가드레일, 카탈로그 권한, 정리."""

import openpyxl
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.datasets.loader import drop_datasets, ingest_if_tabular
from app.datasets.query import (
    SqlGuardError,
    answer_over_dataset,
    maybe_answer_structured,
    validate_sql,
)
from app.datasets.store import DuckDBStore, infer_types, safe_table_name
from app.db.models import Base
from app.db.repositories import DatasetRepository, DocumentRepository
from app.demo.offline import ExtractiveLLM
from app.ingestion.intake import intake
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import DocumentMetadata
from app.search.access import UserContext


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _make_roster(path, n=60):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["사번", "이름", "부서", "급여"])
    for i in range(n):
        ws.append([f"E{i:04d}", f"이름{i}", "인사팀" if i % 2 else "재무팀", 3000 + i])
    wb.save(path)


# ── DuckDB store ─────────────────────────────────────────────────────────────
def test_duckdb_load_and_query(tmp_path):
    store = DuckDBStore(str(tmp_path / "d.duckdb"))
    cols = ["부서", "급여"]
    types = ["VARCHAR", "DOUBLE"]
    rows = [["인사팀", "10"], ["인사팀", "20"], ["재무팀", "30"]]
    store.create_from_rows("t_x", cols, types, rows)
    c, r = store.query('SELECT "부서", sum("급여") AS 합계 FROM "t_x" GROUP BY "부서" ORDER BY "부서"')
    assert c == ["부서", "합계"]
    assert r == [["인사팀", 30.0], ["재무팀", 30.0]]


def test_infer_types():
    header = ["a", "b", "c"]
    rows = [["1", "2025-01-02", "x"], ["2", "2025-03-04", "y"]]
    assert infer_types(header, rows) == ["DOUBLE", "DATE", "VARCHAR"]


# ── text-to-SQL 가드레일 ─────────────────────────────────────────────────────
def test_validate_sql_adds_limit_and_accepts_select():
    out = validate_sql('SELECT * FROM "t_x"', "t_x")
    assert "LIMIT" in out.upper()


def test_validate_sql_rejects_non_select():
    with pytest.raises(SqlGuardError):
        validate_sql('DROP TABLE "t_x"', "t_x")
    with pytest.raises(SqlGuardError):
        validate_sql('INSERT INTO "t_x" VALUES (1)', "t_x")


def test_validate_sql_rejects_other_table_and_multi():
    with pytest.raises(SqlGuardError):
        validate_sql('SELECT * FROM "secret"', "t_x")
    with pytest.raises(SqlGuardError):
        validate_sql('SELECT 1; SELECT 2', "t_x")
    with pytest.raises(SqlGuardError):
        validate_sql('WITH x AS (SELECT * FROM "other") SELECT * FROM x', "t_x")


# ── 적재 → 카탈로그 → 질의 → 답변(end-to-end) ───────────────────────────────
def _register_roster(session, tmp_path, tokens=("*",), n=60):
    xlsx = tmp_path / "salary.xlsx"
    _make_roster(xlsx, n=n)
    from app.schemas.enums import DocStatus
    ident = intake(str(xlsx), ingested_by="t")
    doc = DocumentMetadata(identification=ident)
    doc.governance.access_tokens = list(tokens)
    doc.lifecycle.status = DocStatus.ARCHIVED     # 검색 노출 상태여야 라우팅 대상
    repo = DocumentRepository(session)
    repo.upsert_document(doc, IngestionStatus.INDEXED)
    repo.set_original_path(ident.doc_id, str(xlsx))
    session.commit()
    return doc


def test_ingest_creates_dataset_and_answers(session, tmp_path):
    store = DuckDBStore(str(tmp_path / "d.duckdb"))
    doc = _register_roster(session, tmp_path)
    n = ingest_if_tabular(session, doc, store=store)
    assert n == 1
    ds = DatasetRepository(session).by_doc(doc.identification.doc_id)
    assert len(ds) == 1 and ds[0].row_count == 60

    # 구조화 답변(오프라인 fake LLM = count(*) SQL)
    ctx = UserContext(user_id="u", groups=frozenset())     # 문서가 "*" 공개
    ans = maybe_answer_structured(session, ExtractiveLLM(), ctx, "총 몇 명?",
                                  [doc.identification.doc_id], store=store)
    assert ans is not None
    assert ans["source_doc_id"] == doc.identification.doc_id
    assert "60" in ans["text"]              # count(*)=60
    assert ans["sql"].upper().startswith("SELECT")


def test_list_visible_respects_access(session, tmp_path):
    store = DuckDBStore(str(tmp_path / "d.duckdb"))
    doc = _register_roster(session, tmp_path, tokens=("n:5",))   # 제한 문서
    ingest_if_tabular(session, doc, store=store)
    # 권한 없는 사용자 → 라우팅 대상 없음
    outsider = UserContext(user_id="o", groups=frozenset({"n:9"}))
    assert maybe_answer_structured(session, ExtractiveLLM(), outsider, "몇 명?",
                                   [doc.identification.doc_id], store=store) is None
    # 권한 있는 사용자 → 답변
    insider = UserContext(user_id="i", groups=frozenset({"n:5"}))
    assert maybe_answer_structured(session, ExtractiveLLM(), insider, "몇 명?",
                                   [doc.identification.doc_id], store=store) is not None


def test_drop_datasets_cleans_catalog(session, tmp_path):
    store = DuckDBStore(str(tmp_path / "d.duckdb"))
    doc = _register_roster(session, tmp_path)
    ingest_if_tabular(session, doc, store=store)
    drop_datasets(session, doc.identification.doc_id, store=store)
    assert DatasetRepository(session).by_doc(doc.identification.doc_id) == []
