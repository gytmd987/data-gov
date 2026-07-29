"""표 데이터 질의(Tier 2): 질문 → text-to-SQL → 실행 → 답변.

가드레일: LLM 이 만든 SQL 을 sqlglot 으로 검증(단일 SELECT · 허용 테이블만 · LIMIT 주입)한 뒤
읽기 전용으로 실행한다. 실패하면 None 을 돌려 일반 RAG 로 폴백한다.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.datasets.store import DuckDBStore, quote_ident

_DEFAULT_LIMIT = 1000
_SQL_SCHEMA = {"type": "object",
               "properties": {"sql": {"type": "string"}},
               "required": ["sql"]}


class SqlGuardError(Exception):
    """생성된 SQL 이 안전 규칙을 위반."""


def _rows_md(cols: list[str], rows: list[list[Any]], limit: int = 5) -> str:
    lines = ["| " + " | ".join(cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for r in rows[:limit]:
        lines.append("| " + " | ".join("" if c is None else str(c) for c in r) + " |")
    return "\n".join(lines)


def build_sql_prompt(dataset: dict, question: str,
                     sample_cols: list[str], sample_rows: list[list]) -> str:
    schema = ", ".join(f'"{c["name"]}" {c["type"]}' for c in dataset["columns"])
    return (
        "당신은 DuckDB SQL 생성기입니다. 아래 표 하나에 대한 **단일 SELECT 문**만 만드세요.\n"
        f"TABLE: {dataset['table_name']}\n"
        f"열 스키마: {schema}\n"
        "샘플:\n" + _rows_md(sample_cols, sample_rows) + "\n"
        "규칙:\n"
        f"- 반드시 {dataset['table_name']} 테이블만 사용(다른 테이블/CTE/서브쿼리 금지).\n"
        "- 식별자(테이블·열)는 큰따옴표로 감싸세요(한글 열 이름 포함).\n"
        "- 집계 결과에는 알기 쉬운 한글 별칭(AS)을 붙이세요.\n"
        "- INSERT/UPDATE/DELETE/DDL/PRAGMA 등은 절대 쓰지 마세요.\n"
        f"질문: {question}\n"
    )


def generate_sql(llm, dataset: dict, question: str,
                 sample_cols: list[str], sample_rows: list[list]) -> str:
    prompt = build_sql_prompt(dataset, question, sample_cols, sample_rows)
    out = llm.complete_json(prompt, _SQL_SCHEMA)
    return str((out or {}).get("sql") or "").strip()


def validate_sql(sql: str, allowed_table: str, limit: int = _DEFAULT_LIMIT) -> str:
    """단일 SELECT · 허용 테이블만 참조 확인 + LIMIT 주입. 위반 시 SqlGuardError."""
    import sqlglot
    from sqlglot import exp

    sql = (sql or "").strip().rstrip(";").strip()
    if not sql:
        raise SqlGuardError("빈 SQL")
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except Exception as e:  # noqa: BLE001
        raise SqlGuardError(f"파싱 실패: {e}")
    if len(statements) != 1 or statements[0] is None:
        raise SqlGuardError("단일 문장만 허용")
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise SqlGuardError("SELECT 문만 허용")
    if tree.find(exp.With):
        raise SqlGuardError("CTE(WITH) 금지")
    for tbl in tree.find_all(exp.Table):
        if tbl.name != allowed_table:
            raise SqlGuardError(f"허용되지 않은 테이블 참조: {tbl.name}")
    # 위험 노드 차단(혹시 파서가 관대할 경우 대비)
    for bad in (exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create,
                exp.Command, exp.Alter):
        if tree.find(bad):
            raise SqlGuardError("허용되지 않은 구문")
    if not tree.args.get("limit"):
        tree = tree.limit(limit)
    return tree.sql(dialect="duckdb")


def compose_answer(dataset: dict, cols: list[str], rows: list[list[Any]]) -> str:
    """SQL 결과를 사람이 읽기 쉬운 답변으로(결정적 — LLM 환각 없이 숫자 그대로)."""
    name = dataset.get("title") or dataset.get("filename") or "데이터"
    if len(rows) == 1 and len(cols) == 1:
        return f"'{name}'에서 조회한 결과, {cols[0]} = {rows[0][0]} 입니다."
    head = " | ".join(cols)
    body = "\n".join(" | ".join("" if c is None else str(c) for c in r) for r in rows[:10])
    more = f"\n… 외 {len(rows) - 10}건" if len(rows) > 10 else ""
    return f"'{name}' 조회 결과 {len(rows)}건:\n{head}\n{body}{more}"


def answer_over_dataset(llm, store: DuckDBStore, dataset: dict, question: str
                        ) -> Optional[dict[str, Any]]:
    """대상 데이터셋에 대해 text-to-SQL 실행 → 답변 dict. 실패 시 None."""
    try:
        scols, srows = store.query(
            f"SELECT * FROM {quote_ident(dataset['table_name'])} LIMIT 5")
        sql = generate_sql(llm, dataset, question, scols, srows)
        safe = validate_sql(sql, dataset["table_name"])
        cols, rows = store.query(safe)
    except Exception:  # noqa: BLE001 — 무엇이든 실패하면 일반 RAG 로 폴백
        return None
    if not rows:
        return None
    return {"text": compose_answer(dataset, cols, rows),
            "source_doc_id": dataset["doc_id"],
            "filename": dataset.get("title") or dataset.get("filename"),
            "sql": safe, "columns": cols, "rows": rows[:20]}


def maybe_answer_structured(session: Session, llm, user_ctx, question: str,
                            cited_doc_ids: list[str],
                            store: DuckDBStore | None = None) -> Optional[dict[str, Any]]:
    """검색에 잡힌 문서 중 데이터셋이 있으면 구조화(SQL) 답변을 시도.

    - 권한/검색상태 통과 데이터셋만 대상(list_visible).
    - 검색 순위 순으로 첫 매칭 데이터셋을 사용.
    """
    from app.db.repositories import DatasetRepository
    visible = {d["doc_id"]: d for d in DatasetRepository(session).list_visible(user_ctx)}
    if not visible:
        return None
    chosen = next((visible[d] for d in cited_doc_ids if d in visible), None)
    if chosen is None:
        return None
    return answer_over_dataset(llm, store or DuckDBStore(), chosen, question)
