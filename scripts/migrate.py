"""가벼운 스키마 마이그레이션.

create_all은 '새 테이블'만 만들고 '기존 테이블의 새 컬럼'은 추가하지 않는다.
이 스크립트는 새 테이블 생성 + 이번까지 추가된 컬럼들을 idempotent하게 ADD 한다.

    python -m scripts.migrate
"""

from __future__ import annotations

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
]

# 더 이상 쓰지 않는 컬럼 — NOT NULL 제약이 신규 인원 등록을 막으므로 제거한다.
# (없거나 이미 지워졌으면 조용히 통과)
_DROP_COLUMNS = [
    ("users", "clearance"),
]


def main() -> int:
    engine = make_engine()
    create_all(engine)            # 새 테이블(feedback 등) 생성
    with engine.begin() as conn:
        for table, col, coltype in _COLUMNS:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}"))
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
    print("마이그레이션 완료.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
