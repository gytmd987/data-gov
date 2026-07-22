"""거버넌스 검증 + 적재 차단 로직 (조직도 접근 모델).

접근은 조직도 기반이며 기본값이 '팀 전체'라 별도 필수 입력이 없다. 색인 게이트는
'초안(draft) 상태로는 색인 불가' 하나만 남긴다(active 등으로 확정 필요).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.schemas.enums import DocStatus
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import DocumentMetadata


@dataclass
class ValidationResult:
    ok: bool
    missing_fields: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_status(self) -> IngestionStatus:
        """검증 결과에 따른 다음 적재 상태."""
        return IngestionStatus.VALIDATED if self.ok else IngestionStatus.BLOCKED


def validate_governance(
    doc: DocumentMetadata,
    known_access_groups: Optional[Iterable[str]] = None,   # 하위호환(미사용)
    allowed_topics: Optional[Iterable[str]] = None,        # 하위호환(미사용)
) -> ValidationResult:
    errors: list[str] = []

    # 생애주기: draft 상태로는 색인 불가(active 등으로 확정 필요)
    if doc.lifecycle.status == DocStatus.DRAFT:
        errors.append("status=draft 문서는 색인할 수 없음(active 등으로 확정 필요)")

    return ValidationResult(ok=not errors, errors=errors)
