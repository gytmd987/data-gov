"""접근통제: 사용자 컨텍스트 → 검색 하드 필터.

접근은 조직도 기반 토큰으로 판정한다. 사용자 토큰 `groups`(예: `n:{노드}`, `h:{노드}`)와
문서의 열람 토큰(`access_groups`) 교집합이 있으면 열람 가능. 문서에 `"*"` 가 있으면 팀 전체 공개.

하드 필터 조건(검색 후보에서 원천 제외):
  1. access_groups ∩ 사용자 토큰 ≠ ∅ (문서에 "*" 가 있으면 전체 공개 — 토큰 검사 통과)
  2. status ∈ {active(유효), archived(보관)}  ← 대부분 문서는 보관이라 보관도 검색 노출
  3. expiry_date 없음 또는 오늘 이후(만료 제외)
  4. superseded_by 없음(대체된 문서 제외)

include_past=True 이면 2~4(상태/만료/대체)를 완화해 만료·대체 문서도 검색된다.
단 1(조직 토큰)은 항상 적용되어 권한 없는 문서가 과거라고 뚫리지 않는다.

- to_qdrant_filter(): Qdrant 검색 쿼리에 주입할 필터(후보 단계에서 배제).
- allows(payload): 동일 규칙의 파이썬 재검증 → 답변 인용 직전 방어적 이중 체크에 사용.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional


@dataclass(frozen=True)
class UserContext:
    user_id: str
    groups: frozenset[str]        # 조직 토큰(n:{노드} + 부서장이면 h:{노드})


@dataclass(frozen=True)
class Visibility:
    """관리 화면(목록·상세·문서 검색)에서 이 사용자에게 보여도 되는 문서의 범위.

    - read_tokens: 사용자의 조직 토큰. 문서 열람 토큰과 겹치면 볼 수 있다.
    - manage_node_ids: 부서장이 관리하는 작성부서(subtree). 열람 토큰과 무관하게
      자기 부서 문서는 관리해야 하므로 함께 허용한다.
    - unrestricted: 관리자 — 제한 없음.

    채팅 검색은 AccessPolicy(토큰+생애주기)를 쓰고, 이쪽은 '관리 화면에서 보이는 범위'다.
    둘 다 DB/Qdrant 단에서 거르며, 화면 렌더 단계에서 거르지 않는다.
    """

    read_tokens: frozenset[str] = frozenset()
    manage_node_ids: frozenset[int] = frozenset()
    unrestricted: bool = False

    @classmethod
    def admin(cls) -> "Visibility":
        return cls(unrestricted=True)

    def allows_tokens(self, doc_tokens) -> bool:
        """이 문서의 열람 토큰으로 볼 수 있는지(생애주기 무관, 순수 권한 판정)."""
        if self.unrestricted:
            return True
        tokens = set(doc_tokens or []) or {"*"}
        return "*" in tokens or bool(tokens & set(self.read_tokens))


@dataclass(frozen=True)
class AccessPolicy:
    user: UserContext
    today: date
    include_past: bool = False   # True 면 만료/대체/비active 문서도 허용(권한·민감도는 유지)
    # 폴더(조직노드) 스코프 — 지정하면 그 노드들에 속한 문서만. None 이면 전체.
    folder_node_ids: Optional[frozenset[int]] = None

    @classmethod
    def for_user(cls, user: UserContext, today: Optional[date] = None,
                 include_past: bool = False,
                 folder_node_ids: Optional[frozenset[int]] = None) -> "AccessPolicy":
        return cls(user=user, today=today or date.today(), include_past=include_past,
                   folder_node_ids=folder_node_ids)

    # ── 파이썬 재검증(방어적 이중 체크) ────────────────────────────────────
    def allows(self, payload: dict[str, Any]) -> bool:
        # 1. 조직 토큰 교집합 ("*" = 팀 전체 공개 — 토큰 제한 없음)
        groups = payload.get("access_groups") or []
        if "*" not in groups and not (set(groups) & self.user.groups):
            return False
        # 0. 폴더 스코프(선택) — 지정된 폴더(하위 포함) 문서만
        if self.folder_node_ids is not None:
            if payload.get("author_node_id") not in self.folder_node_ids:
                return False
        # 과거 문서 포함 모드: 상태/만료/대체 검사는 생략(조직 토큰은 위에서 이미 적용)
        if self.include_past:
            return True
        # 2. 상태 — 기본 검색은 '유효'와 '보관'을 노출(만료·대체·초안 제외)
        if payload.get("status") not in ("active", "archived"):
            return False
        # 3. 만료
        expiry = payload.get("expiry_date")
        if expiry:
            try:
                if date.fromisoformat(expiry[:10]) < self.today:
                    return False
            except ValueError:
                return False
        # 4. 대체
        if payload.get("superseded_by"):
            return False
        return True

    # ── Qdrant 필터 ─────────────────────────────────────────────────────────
    def to_qdrant_filter(self):
        """qdrant_client.models.Filter 반환(런타임 import로 의존 격리).

        후보를 크게 줄이는 신뢰 가능한 조건(조직 토큰·상태)만 Qdrant에 건다.
        만료(expiry_date)와 대체(superseded_by)는 payload가 ISO 문자열이라 range 필터가
        취약하므로 allows() 파이썬 재검증에서 확정 배제한다(검색기는 항상 allows()를 후처리로 적용).
        """
        from qdrant_client import models as qm

        must = [
            qm.FieldCondition(
                key="access_groups",
                match=qm.MatchAny(any=[*self.user.groups, "*"]),
            ),
        ]
        # 과거 문서 포함 모드가 아니면 '유효+보관' 상태만 후보로(기본 동작)
        if not self.include_past:
            must.append(qm.FieldCondition(
                key="status", match=qm.MatchAny(any=["active", "archived"])))
        # 폴더 스코프(선택) — 상위 폴더 선택 시 호출측에서 subtree 를 펼쳐 전달한다
        if self.folder_node_ids:
            must.append(qm.FieldCondition(
                key="author_node_id",
                match=qm.MatchAny(any=sorted(self.folder_node_ids))))
        return qm.Filter(must=must)
