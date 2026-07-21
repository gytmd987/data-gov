"""접근통제: 사용자 컨텍스트 → 검색 하드 필터.

하드 필터 조건(검색 후보에서 원천 제외):
  1. access_groups ∩ 사용자 그룹 ≠ ∅
  2. sensitivity_rank ≤ 사용자 clearance
  3. status == active
  4. expiry_date 없음 또는 오늘 이후(만료 제외)
  5. superseded_by 없음(대체된 문서 제외)

- to_qdrant_filter(): Qdrant 검색 쿼리에 주입할 필터(후보 단계에서 배제).
- allows(payload): 동일 규칙의 파이썬 재검증 → 답변 인용 직전 방어적 이중 체크에 사용.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from app import system_config
from app.schemas.enums import SensitivityLevel


@dataclass(frozen=True)
class UserContext:
    user_id: str
    groups: frozenset[str]
    clearance: SensitivityLevel


@dataclass(frozen=True)
class AccessPolicy:
    user: UserContext
    today: date

    @classmethod
    def for_user(cls, user: UserContext, today: Optional[date] = None) -> "AccessPolicy":
        return cls(user=user, today=today or date.today())

    # ── 파이썬 재검증(방어적 이중 체크) ────────────────────────────────────
    def allows(self, payload: dict[str, Any]) -> bool:
        # 1. 그룹 교집합
        groups = payload.get("access_groups") or []
        if not (set(groups) & self.user.groups):
            return False
        # 2. 민감도
        rank = payload.get("sensitivity_rank")
        if rank is None or rank > system_config.sensitivity_rank(self.user.clearance):
            return False
        # 3. 상태
        if payload.get("status") != "active":
            return False
        # 4. 만료
        expiry = payload.get("expiry_date")
        if expiry:
            try:
                if date.fromisoformat(expiry[:10]) < self.today:
                    return False
            except ValueError:
                return False
        # 5. 대체
        if payload.get("superseded_by"):
            return False
        return True

    # ── Qdrant 필터 ─────────────────────────────────────────────────────────
    def to_qdrant_filter(self):
        """qdrant_client.models.Filter 반환(런타임 import로 의존 격리).

        후보를 크게 줄이는 신뢰 가능한 조건(그룹·민감도·상태)만 Qdrant에 건다.
        만료(expiry_date)와 대체(superseded_by)는 payload가 ISO 문자열이라 range 필터가
        취약하므로 allows() 파이썬 재검증에서 확정 배제한다(검색기는 항상 allows()를 후처리로 적용).
        """
        from qdrant_client import models as qm

        return qm.Filter(
            must=[
                qm.FieldCondition(
                    key="access_groups",
                    match=qm.MatchAny(any=list(self.user.groups)),
                ),
                qm.FieldCondition(
                    key="sensitivity_rank",
                    range=qm.Range(lte=system_config.sensitivity_rank(self.user.clearance)),
                ),
                qm.FieldCondition(key="status", match=qm.MatchValue(value="active")),
            ],
        )
