"""가벼운 스키마 마이그레이션.

create_all은 '새 테이블'만 만들고 '기존 테이블의 새 컬럼'은 추가하지 않는다.
이 스크립트는 새 테이블 생성 + 이번까지 추가된 컬럼들을 idempotent하게 ADD 한다.

    python -m scripts.migrate
"""

from __future__ import annotations

import json

from sqlalchemy import text

from app.db.session import create_all, make_engine

# (table, column, type) — ADD COLUMN IF NOT EXISTS
_COLUMNS = [
    ("documents", "similar_candidates", "JSON DEFAULT '[]'"),
    ("documents", "original_path", "VARCHAR(1024)"),
    ("users", "position", "VARCHAR(64)"),
    ("users", "job", "VARCHAR(64)"),
    ("users", "org_node_id", "INTEGER"),
    ("users", "org_role", "VARCHAR(16)"),
    ("documents", "author_node_id", "INTEGER"),
    ("org_nodes", "leader_id", "VARCHAR(128)"),
    ("org_nodes", "default_access", "JSON DEFAULT '[]'"),
    ("documents", "message_id", "VARCHAR(512)"),
    ("documents", "in_reply_to", "VARCHAR(512)"),
    ("documents", "thread_root", "VARCHAR(512)"),
    ("upload_jobs", "start_after", "TIMESTAMPTZ"),
    ("upload_jobs", "bypass_window", "BOOLEAN DEFAULT FALSE"),
]

# 더 이상 쓰지 않는 컬럼 — NOT NULL 제약이 신규 인원 등록을 막으므로 제거한다.
# (없거나 이미 지워졌으면 조용히 통과)
_DROP_COLUMNS = [
    ("users", "clearance"),
]


def _backfill_access_tokens(engine) -> int:
    """documents.access_groups → document_access_tokens 백필(멱등).

    이 테이블이 비어 있으면 목록·문서검색이 권한 필터로 아무것도 못 찾으므로,
    새 테이블 생성 후 반드시 한 번 채워야 한다.
    """
    from sqlalchemy.orm import Session

    from app.db.models import Document
    from app.db.repositories import DocumentRepository

    n = 0
    with Session(engine) as s:
        repo = DocumentRepository(s)
        for doc_id, groups in s.execute(
                text("SELECT doc_id, access_groups FROM documents")).all():
            tokens = groups if isinstance(groups, list) else json.loads(groups or "[]")
            repo.sync_access_tokens(doc_id, tokens)
            n += 1
        s.commit()
    _ = Document   # (모델 임포트로 테이블 메타데이터 등록 보장)
    return n


def _fix_email_extension(engine) -> int:
    """예전에 보관한 메일 원본의 확장자 `.email` → `.eml` 로 고친다(멱등).

    보관 파일명을 내부 형식 값(email)으로 만들던 시절의 잔재다. `.email` 은 윈도우가
    모르는 확장자라 다운로드해도 더블클릭으로 안 열린다.
    """
    import os

    from sqlalchemy.orm import Session

    n = 0
    with Session(engine) as s:
        rows = s.execute(text(
            "SELECT doc_id, original_path FROM documents "
            "WHERE original_path LIKE '%.email'")).all()
        for doc_id, path in rows:
            new_path = path[: -len(".email")] + ".eml"
            try:
                if os.path.exists(path) and not os.path.exists(new_path):
                    os.rename(path, new_path)
            except OSError:
                continue
            s.execute(text("UPDATE documents SET original_path = :p WHERE doc_id = :d"),
                      {"p": new_path, "d": doc_id})
            n += 1
        s.commit()
    return n


def _fix_timestamptz(engine) -> int:
    """upload_jobs.start_after 를 timestamptz 로 맞춘다(Postgres 전용, 멱등).

    모델은 DateTime(timezone=True) 인데 초기 마이그레이션이 TIMESTAMP(시간대 없음)로
    만들어 둔 적이 있다. 세션 시간대에 따라 예약 시각 비교가 어긋날 수 있다.
    """
    if engine.dialect.name != "postgresql":
        return 0
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE upload_jobs ALTER COLUMN start_after "
                "TYPE TIMESTAMPTZ USING start_after AT TIME ZONE 'UTC'"))
        return 1
    except Exception:      # 이미 timestamptz 이거나 테이블이 없음
        return 0


def main(engine=None) -> int:
    engine = engine or make_engine()   # engine 주입은 테스트용
    create_all(engine)            # 새 테이블(feedback, document_access_tokens, upload_jobs 등) 생성
    with engine.begin() as conn:
        for table, col, coltype in _COLUMNS:
            # Postgres 는 IF NOT EXISTS 지원, SQLite 는 미지원 → 순차 시도.
            # (그냥 ADD COLUMN 은 이미 있으면 에러 = 이미 마이그레이션된 상태)
            added = False
            for sql in (f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}",
                        f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"):
                try:
                    conn.execute(text(sql))
                    added = True
                    break
                except Exception:
                    continue
            print(f"  {'OK' if added else 'skip(있음)'}: {table}.{col}")
        for table, col in _DROP_COLUMNS:
            # Postgres 는 IF EXISTS 지원, SQLite(3.35+) 는 미지원 → 순차 시도.
            dropped = False
            for sql in (f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}",
                        f"ALTER TABLE {table} DROP COLUMN {col}"):
                try:
                    conn.execute(text(sql))
                    dropped = True
                    break
                except Exception:
                    continue
            print(f"  {'OK(drop)' if dropped else 'skip(drop)'}: {table}.{col}")
    n = _backfill_access_tokens(engine)
    print(f"  OK: document_access_tokens 백필 {n}건")
    if _fix_timestamptz(engine):
        print("  OK: upload_jobs.start_after → TIMESTAMPTZ")
    fixed = _fix_email_extension(engine)
    if fixed:
        print(f"  OK: 메일 원본 확장자 .email → .eml {fixed}건")
    print("마이그레이션 완료.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
