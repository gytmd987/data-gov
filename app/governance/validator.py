"""거버넌스 검증 + 적재 차단 로직.

핵심 규칙: 거버넌스 필수 필드가 확정되지 않으면 INDEXED로 진행할 수 없다(BLOCKED).
검증은 PENDING_REVIEW → VALIDATED 전이의 게이트로 사용한다.

필수 거버넌스 필드:
  - sensitivity_level (enum, 값 존재)
  - contains_pii (bool 명시), contains_pii=True 이면 pii_types 최소 1개
  - access_groups (최소 1개, 알려진 그룹이어야 함; "*" 는 전체 공개 센티널로 항상 허용)
  - owner (값 존재)
  - lifecycle.status (draft 가 아니어야 색인 가능)

topics 는 controlled tag 사전(allowed_topics) 안에 있어야 한다(사전이 주어진 경우).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app import system_config
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


# Phase 0에서 인사팀과 확정할 그룹/토픽 사전. 비어 있으면 해당 검사는 생략한다.
def validate_governance(
    doc: DocumentMetadata,
    known_access_groups: Optional[Iterable[str]] = None,
    allowed_topics: Optional[Iterable[str]] = None,
) -> ValidationResult:
    missing: list[str] = []
    errors: list[str] = []

    gov = doc.governance
    life = doc.lifecycle

    # 필수 필드 목록은 config/system.yaml(governance.required_fields)에서 온다.
    required = set(system_config.required_governance_fields())
    # 사전이 안 주어지면 config의 값을 기본값으로 사용.
    if known_access_groups is None:
        known_access_groups = system_config.access_groups()
    if allowed_topics is None:
        allowed_topics = system_config.topics() or None  # 비어 있으면 검증 생략

    # 1) 필수 거버넌스 필드 존재 여부 (config에 나열된 것만)
    if "sensitivity_level" in required and gov.sensitivity_level is None:
        missing.append("governance.sensitivity_level")
    if "contains_pii" in required and gov.contains_pii is None:
        missing.append("governance.contains_pii")
    if "access_groups" in required and not gov.access_groups:
        missing.append("governance.access_groups")
    if "owner" in required and not gov.owner:
        missing.append("governance.owner")

    # 2) PII 일관성: PII 포함이면 유형 최소 1개
    if gov.contains_pii is True and not gov.pii_types:
        errors.append("contains_pii=True 인데 pii_types 가 비어 있음")

    # 3) access_groups 실재 여부 ("*" 는 전체 공개 센티널 — 항상 유효)
    if known_access_groups is not None and gov.access_groups:
        known = set(known_access_groups) | {"*"}
        unknown = [g for g in gov.access_groups if g not in known]
        if unknown:
            errors.append(f"알 수 없는 access_groups: {unknown}")

    # 4) topics controlled tag 검증
    if allowed_topics is not None and doc.classification.topics:
        allowed = set(allowed_topics)
        invalid = [t for t in doc.classification.topics if t not in allowed]
        if invalid:
            errors.append(f"허용되지 않은 topics(자유 태그 금지): {invalid}")

    # 5) 생애주기: draft 상태로는 색인 불가
    if life.status == DocStatus.DRAFT:
        errors.append("status=draft 문서는 색인할 수 없음(active 등으로 확정 필요)")

    ok = not missing and not errors
    return ValidationResult(ok=ok, missing_fields=missing, errors=errors)
