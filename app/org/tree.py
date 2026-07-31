"""조직도 트리 헬퍼 + 접근 토큰 계산 (순수 함수 — DB 비의존, 테스트 가능).

접근제어는 문자열 토큰의 교집합으로 판정한다(기존 access_groups MatchAny 재사용):
- 사용자 토큰: `n:{내 노드}` + (부서장이면) `h:{내 노드}`
- 문서 열람 토큰(access_selections → readable_tokens):
    - `node:N`  → subtree(N) 각 M에 `n:M`(부서 전원) + ancestors(N) 각 A에 `h:A`(상위 부서장)
    - `head:N`  → `h:N`(그 부서장) + ancestors(N) 각 A에 `h:A`
    - 빈 선택   → `["*"]` (팀 전체 공개)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional

# 부서장(head) 역할 — 파트원만 비-head
HEAD_ROLES = frozenset({"팀장", "그룹장", "파트장"})
ROLES = ("팀장", "그룹장", "파트장", "파트원")
NODE_TYPES = ("team", "group", "part")


@dataclass(frozen=True)
class OrgNodeView:
    id: int
    name: str
    node_type: str
    parent_id: Optional[int]


def is_head_role(role: Optional[str]) -> bool:
    return role in HEAD_ROLES


def user_tokens(node_id: Optional[int], role: Optional[str]) -> list[str]:
    """사용자가 보유한 접근 토큰."""
    if node_id is None:
        return []
    tokens = [f"n:{node_id}"]
    if is_head_role(role):
        tokens.append(f"h:{node_id}")
    return tokens


class OrgTree:
    def __init__(self, nodes: Iterable[OrgNodeView]) -> None:
        self._by_id: dict[int, OrgNodeView] = {}
        self._children: dict[int, list[int]] = defaultdict(list)
        for n in nodes:
            self._by_id[n.id] = n
        for n in self._by_id.values():
            if n.parent_id is not None and n.parent_id in self._by_id:
                self._children[n.parent_id].append(n.id)

    def get(self, node_id: int) -> Optional[OrgNodeView]:
        return self._by_id.get(node_id)

    def node_ids(self) -> list[int]:
        """모든 노드 id."""
        return list(self._by_id)

    def ancestors(self, node_id: int) -> list[int]:
        """자기 제외, 부모부터 위로."""
        out: list[int] = []
        cur = self._by_id.get(node_id)
        seen = {node_id}
        while cur is not None and cur.parent_id is not None and cur.parent_id not in seen:
            out.append(cur.parent_id)
            seen.add(cur.parent_id)
            cur = self._by_id.get(cur.parent_id)
        return out

    def name_path(self, node_id: int) -> list[str]:
        """루트부터 이 노드까지의 이름 경로(저장 폴더 경로 구성용). 없으면 빈 목록."""
        node = self._by_id.get(node_id)
        if node is None:
            return []
        names = [node.name]
        for a in self.ancestors(node_id):
            anc = self._by_id.get(a)
            if anc is not None:
                names.append(anc.name)
        return list(reversed(names))

    def subtree(self, node_id: int) -> list[int]:
        """자기 포함, 모든 하위."""
        if node_id not in self._by_id:
            return []
        out = [node_id]
        stack = [node_id]
        while stack:
            cur = stack.pop()
            for child in self._children.get(cur, []):
                out.append(child)
                stack.append(child)
        return out

    def readable_tokens(self, selections: Iterable[str]) -> list[str]:
        """문서 access_selections → 열람 허용 토큰 목록. 빈 선택은 팀 전체(`*`).

        지금 화면에서 고를 수 있는 건 `node:<id>`(부서) 하나뿐이다.
        `head:<id>`(그 부서장만)는 UI에서 제거됐지만, 예전에 그렇게 저장된 문서가
        그대로 동작하도록 해석은 유지한다.
        """
        selections = [s for s in (selections or []) if s]
        if not selections:
            return ["*"]
        tokens: set[str] = set()
        for sel in selections:
            kind, _, raw = sel.partition(":")
            try:
                nid = int(raw)
            except ValueError:
                continue
            if nid not in self._by_id:
                continue
            if kind == "node":
                for m in self.subtree(nid):
                    tokens.add(f"n:{m}")
            elif kind == "head":
                tokens.add(f"h:{nid}")
            else:
                continue
            for a in self.ancestors(nid):
                tokens.add(f"h:{a}")
        return sorted(tokens)
