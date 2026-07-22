"""만료 문서 자동 정리(배치).

만료일(expiry_date)이 지났는데도 active 상태인 문서를 EXPIRED 로 전환하고
Qdrant payload 를 동기화한다. cron 에 등록해 매일 1회 실행을 권장한다.

    python -m scripts.lifecycle_sweep

실서비스(vLLM/TEI/Qdrant/Postgres)는 기존 .env 를 그대로 사용한다.
"""

from __future__ import annotations

import sys

from app.manage.lifecycle import sweep_expired
from app.review.factory import build_document_manager, new_session


def main() -> int:
    session = new_session()
    try:
        manager = build_document_manager(session)
        expired = sweep_expired(manager)
        print(f"✅ 만료 처리: {len(expired)}건" + (f" — {expired}" if expired else ""))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
