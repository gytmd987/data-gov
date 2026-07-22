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
]


def main() -> int:
    engine = make_engine()
    create_all(engine)            # 새 테이블(feedback 등) 생성
    with engine.begin() as conn:
        for table, col, coltype in _COLUMNS:
            conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {coltype}"))
            print(f"  OK: {table}.{col}")
    print("마이그레이션 완료.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
