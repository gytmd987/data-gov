"""생애주기 자동화 — 만료일이 지난 문서를 자동으로 expired 처리.

현재 만료는 검색 시점에 expiry_date < today 로 판정되지만, 문서의 lifecycle_status
자체는 계속 active 로 남아 관리 화면에서 혼동을 준다. 이 sweep 은 만료일이 지난
active 문서의 상태를 EXPIRED 로 확정하고 Qdrant payload 도 동기화한다(검색 하드필터 일치).

- 배치(cron): `python -m scripts.lifecycle_sweep`
- 관리 콘솔의 "만료 문서 정리" 버튼에서도 호출.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from app.manage.service import DocumentManager
from app.schemas.enums import DocStatus


def sweep_expired(manager: DocumentManager, today: Optional[date] = None) -> list[str]:
    """만료일이 지난 active 문서를 EXPIRED 로 전환. 전환한 doc_id 목록을 반환."""
    today = today or date.today()
    doc_ids = manager.repo.doc_ids_to_expire(today)
    for doc_id in doc_ids:
        manager.set_status(doc_id, DocStatus.EXPIRED)   # _save 가 Qdrant payload 동기화
    return doc_ids
