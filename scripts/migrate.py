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


def main(engine=None) -> int:
    engine = engine or make_engine()   # engine 주입은 테스트용
    create_all(engine)            # 새 테이블(feedback, document_access_tokens, upload_jobs 등) 생성
    with engine.begin() as conn:
        for table, col, coltype in _COLUMNS:
            try:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}"))
            except Exception:      # 구버전 SQLite 는 IF NOT EXISTS 미지원
                continue
            print(f"  OK: {table}.{col}")
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
    print("마이그레이션 완료.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
