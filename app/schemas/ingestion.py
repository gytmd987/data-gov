"""적재 상태 머신.

    UPLOADED → PARSED → AUTO_ENRICHED → PENDING_REVIEW → VALIDATED → INDEXED
                                             │
                                             └─(거버넌스 필수 필드 미충족)→ BLOCKED

BLOCKED 상태는 사람이 필드를 보정하면 다시 PENDING_REVIEW 로 돌아가 재검증할 수 있다.
INDEXED 이전에는 어떤 청크도 검색 인덱스(Qdrant)에 올라가지 않는다.
"""

from __future__ import annotations

from enum import Enum


class IngestionStatus(str, Enum):
    UPLOADED = "uploaded"             # 파일 수신 + 시스템 자동 필드 채움 + 중복 탐지
    PARSED = "parsed"                 # 포맷별 파싱(텍스트/표/이미지) 완료
    AUTO_ENRICHED = "auto_enriched"   # LLM이 controlled schema 내에서 자동 채움
    PENDING_REVIEW = "pending_review" # human-in-the-loop 검토 대기
    VALIDATED = "validated"           # 거버넌스 필수 필드 검증 통과
    INDEXED = "indexed"               # 임베딩 + Qdrant upsert 완료 (검색 노출)
    BLOCKED = "blocked"               # 거버넌스 미충족으로 적재 차단


# 허용된 상태 전이. 여기 없는 전이는 오류로 처리한다.
_ALLOWED_TRANSITIONS: dict[IngestionStatus, set[IngestionStatus]] = {
    IngestionStatus.UPLOADED: {IngestionStatus.PARSED},
    IngestionStatus.PARSED: {IngestionStatus.AUTO_ENRICHED},
    IngestionStatus.AUTO_ENRICHED: {IngestionStatus.PENDING_REVIEW},
    IngestionStatus.PENDING_REVIEW: {IngestionStatus.VALIDATED, IngestionStatus.BLOCKED},
    IngestionStatus.VALIDATED: {IngestionStatus.INDEXED, IngestionStatus.PENDING_REVIEW},
    IngestionStatus.BLOCKED: {IngestionStatus.PENDING_REVIEW},
    IngestionStatus.INDEXED: set(),   # 종료 상태
}


class IngestionTransitionError(ValueError):
    """허용되지 않은 상태 전이 시도."""


def can_transition(current: IngestionStatus, target: IngestionStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, set())


def assert_transition(current: IngestionStatus, target: IngestionStatus) -> None:
    if not can_transition(current, target):
        raise IngestionTransitionError(
            f"허용되지 않은 적재 상태 전이: {current.value} → {target.value}"
        )
