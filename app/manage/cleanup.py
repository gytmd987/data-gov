"""폴더 정리 추천 — '버전만 다른 문서'를 묶어 사람에게 보여준다.

대량 업로드나 여러 사람이 올리다 보면 같은 문서의 다른 버전이 쌓인다. 제목이 서로
달라서(예: `연차규정.docx` / `휴가지침_최종.docx` / `[인사팀] 연차 운영안 v3.docx`)
목록만 봐서는 관계를 알 수 없고, 올린 사람도 제각각이라 아무도 눈치채지 못한다.

**새로 판정하지 않는다.** 적재할 때 이미 임베딩 유사도로 후보를 뽑고 LLM 이 관계를
판정해 `documents.similar_candidates` 에 넣어 뒀다. 즉시 처리 경로는 검토 화면에서
그걸 쓰지만, 예약·일괄 반입은 검토를 건너뛰므로 그 판정이 그대로 버려진다.
여기서는 저장된 값을 읽어 **그룹으로 묶기만** 한다(LLM 호출 0, 임베딩 0).

정리 여부는 사람이 정한다. 자동으로 지우거나 교체하지 않는다 — 서식이 같은 정기
문서(월간 보고서 등)가 개정판으로 잘못 잡히는 일이 실제로 있기 때문이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.ingestion.titletools import extract_date, strip_date_prefix
from app.relations.detect import DUP_THRESHOLD

# 한 그룹에 이보다 많이 묶이면 서식이 같은 문서 뭉치일 가능성이 크다 → 사람이 더 주의
CROWDED_GROUP = 6


@dataclass
class CleanupGroup:
    """겹치는 문서 한 묶음."""

    docs: list[dict[str, Any]] = field(default_factory=list)
    min_score: float = 0.0
    max_score: float = 0.0
    periodic: bool = False        # 정기 문서로 보임(날짜만 다른 같은 제목) → 정리 주의
    reason: str = ""

    @property
    def size(self) -> int:
        return len(self.docs)

    @property
    def suggested_keep(self) -> Optional[str]:
        """최신본 후보 — 작성일(없으면 등록일)이 가장 늦은 것. **제안일 뿐이다.**"""
        return self.docs[0]["doc_id"] if self.docs else None


def _looks_periodic(docs: list[dict[str, Any]]) -> bool:
    """정기 문서인가 — 날짜를 뗀 제목이 같은데 **날짜가 서로 다르면** 그렇다.

    월간 보고서처럼 서식이 같고 숫자만 다른 문서는 내용 유사도가 개정판만큼 높다.
    이걸 개정판으로 처리하면 지난 달 보고서가 사라지므로 반드시 구분해야 한다.
    """
    bases, dates = set(), set()
    for d in docs:
        title = d.get("title") or d.get("filename") or ""
        found, rest = extract_date(title)       # (날짜, 날짜를 뺀 나머지) 를 돌려준다
        base = strip_date_prefix(rest if found else title)
        bases.add(_squash(base))
        dates.add(str(found) if found else "")
    real_dates = {x for x in dates if x}
    return len(bases) == 1 and len(real_dates) >= 2


def _squash(text: str) -> str:
    """제목 비교용 정규화 — 날짜를 떼면 남는 공백·구분자 차이를 지운다."""
    return re.sub(r"[\s_\-.]+", " ", text or "").strip().lower()


class _Union:
    """서로 겹치는 문서를 한 묶음으로 — A↔B, B↔C 면 A·B·C 가 한 그룹."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def build_groups(docs: Iterable[dict[str, Any]],
                 candidates: dict[str, list[dict[str, Any]]],
                 threshold: float = DUP_THRESHOLD) -> list[CleanupGroup]:
    """문서 목록 + 저장된 후보 → 겹치는 그룹들.

    docs        : [{doc_id, title, filename, author, effective_date, created_at}, …]
    candidates  : {doc_id: [{doc_id, score, ai_relation}, …]}  (DB 에 저장돼 있던 값)

    **이 목록 안에 있는 문서끼리만** 묶는다. 호출하는 쪽이 이미 권한·폴더로 걸러
    넘기므로, 볼 수 없는 문서가 그룹에 섞이지 않는다.
    """
    doc_list = list(docs)
    by_id = {d["doc_id"]: d for d in doc_list}
    # 들어온 순서(최근 수정순)를 동점일 때의 기준으로 쓴다 — 목록 순서와 어긋나지 않게
    rank = {d["doc_id"]: i for i, d in enumerate(doc_list)}
    uf = _Union()
    scores: dict[str, list[float]] = {}
    relations: dict[str, set[str]] = {}

    for doc_id, cands in candidates.items():
        if doc_id not in by_id:
            continue
        for c in cands or []:
            other = c.get("doc_id")
            score = float(c.get("score") or 0.0)
            if other not in by_id or other == doc_id or score < threshold:
                continue
            uf.union(doc_id, other)
            key = uf.find(doc_id)
            scores.setdefault(key, []).append(score)
            if c.get("ai_relation"):
                relations.setdefault(key, set()).add(str(c["ai_relation"]))

    members: dict[str, list[str]] = {}
    for doc_id in by_id:
        members.setdefault(uf.find(doc_id), []).append(doc_id)

    out: list[CleanupGroup] = []
    for root, ids in members.items():
        if len(ids) < 2:
            continue                       # 혼자면 정리할 게 없다
        # 최신본 후보가 맨 앞에 오도록 — 작성일이 늦은 것, 같으면 최근에 손댄 것
        ordered = sorted((by_id[i] for i in ids),
                         key=lambda d: (str(d.get("effective_date") or ""),
                                        -rank[d["doc_id"]]),
                         reverse=True)
        got = _collect_scores(scores, uf, ids, root)
        group = CleanupGroup(
            docs=ordered,
            min_score=round(min(got), 3) if got else 0.0,
            max_score=round(max(got), 3) if got else 0.0,
            periodic=_looks_periodic(ordered),
        )
        group.reason = _describe(group, relations.get(root, set()))
        out.append(group)

    # 확실한 것(유사도 높은 것)부터 위로
    out.sort(key=lambda g: (g.periodic, -g.max_score, -g.size))
    return out


def _collect_scores(scores, uf, ids, root) -> list[float]:
    got = list(scores.get(root, []))
    for i in ids:                          # union 과정에서 다른 뿌리에 붙은 점수 회수
        got.extend(scores.get(i, []))
    return got


def _describe(group: CleanupGroup, relations: set[str]) -> str:
    parts = [f"내용 유사도 {group.min_score:.2f}~{group.max_score:.2f}"]
    if "revision" in relations:
        parts.append("AI 는 개정판 관계로 봄")
    if group.periodic:
        parts.append("⚠️ 날짜만 다른 정기 문서로 보임 — 개정판이 아닐 수 있음")
    elif group.size >= CROWDED_GROUP:
        parts.append(f"⚠️ {group.size}건이 한 묶음 — 서식이 같은 문서일 수 있음")
    return " · ".join(parts)
