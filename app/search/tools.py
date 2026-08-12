"""LLM 에게 줄 검색 도구 — 질문을 어떻게 찾을지 LLM 이 스스로 정하게 한다.

지금까지는 파이프라인이 고정돼 있었다: 질문 → 의미 검색 1회 → 조각 6개 → 답변.
그래서 "최근 그룹장한테 보고한 문서" 처럼 **조건으로 찾아야 하는 질문**이나,
"이 법이 바뀌었는데 영향받는 규정 다 찾아줘" 처럼 **전수로 훑어야 하는 질문**은
구조상 답할 수가 없었다. 질문 종류마다 코드를 다는 대신, 도구를 주고 계획은 LLM 이
짜게 한다.

도구는 넷이면 충분하고, 넷 다 이미 있던 함수를 감싼 것이다.

  문서_세기   : 조건에 맞는 문서가 몇 건인가 → 전수로 갈지 좁힐지 먼저 판단
  문서_목록   : 메타데이터(종류·작성자·기간)로 목록 뽑기  ← 의미 검색으로는 안 되던 일
  내용_검색   : 의미로 본문 조각 찾기(지금까지의 그 검색)
  문서_읽기   : 특정 문서를 자세히 보기

**권한(중요).** 도구는 사용자를 인자로 받지 않는다. 호출하는 쪽(이 파일)이 붙인다.
LLM 이 만든 조건은 **AND 로만** 붙어 범위를 좁히기만 하고, 넓히지 못한다.
LLM 에게 열람 토큰을 보여주지도 않으므로 프롬프트로 우회할 대상 자체가 없다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from app.schemas.metadata import doc_level_payload
from app.search.access import AccessPolicy, Visibility
from app.search.types import RetrievedChunk

# 한 번에 돌려줄 최대치 — 넘기면 프롬프트가 터지고 모델이 길을 잃는다
MAX_LIST = 60
MAX_SEARCH = 12
COUNT_CAP = 500            # 이보다 많으면 세지 않고 '너무 많다'고 알려 준다
MAX_DOC_CHARS = 4000
SNIPPET_CHARS = 600


@dataclass
class ToolResult:
    """도구 실행 결과 — LLM 에게 보여줄 텍스트 + 인용 가능한 근거."""

    text: str
    chunks: list[RetrievedChunk] = field(default_factory=list)
    note: str = ""                       # 사람에게 보여줄 진행 상황 한 줄


@dataclass
class ToolBox:
    """사용자 한 명에게 묶인 도구 모음. 권한은 여기서 붙고 LLM 은 손댈 수 없다."""

    session: Any
    policy: AccessPolicy
    visibility: Visibility
    retrieve: Callable[[str, AccessPolicy, int], list[RetrievedChunk]]
    today: date
    folder_node_ids: Optional[frozenset[int]] = None

    # ── 도구 1. 문서 세기 ───────────────────────────────────────────────────
    def count_documents(self, **cond) -> ToolResult:
        """규모를 재는 도구 — 전수로 훑을지 좁힐지 LLM 이 먼저 판단하게 한다.

        SQL 로 세면 만료·대체 문서까지 세어 `문서_목록` 결과와 어긋난다. 실제로
        훑을 목록과 같은 규칙으로 세야 "47건이니 다 볼 만하다"는 판단이 맞는다.
        """
        docs = self._readable_rows(cond, COUNT_CAP + 1)
        if len(docs) > COUNT_CAP:
            return ToolResult(text=f"조건에 맞는 문서: {COUNT_CAP}건 이상 "
                                   "(너무 많아 전부 훑을 수 없습니다. 범위를 좁히세요)",
                              note=f"문서 {COUNT_CAP}건 이상")
        return ToolResult(text=f"조건에 맞는 문서: {len(docs)}건",
                          note=f"문서 {len(docs)}건 확인")

    # ── 도구 2. 문서 목록 ───────────────────────────────────────────────────
    def list_documents(self, **cond) -> ToolResult:
        limit = min(int(cond.get("개수") or 30), MAX_LIST)
        docs = self._readable_rows(cond, limit)
        if not docs:
            return ToolResult(text="조건에 맞는 문서가 없습니다.", note="0건")

        lines, chunks = [], []
        for row, doc in docs:
            summary = doc.classification.summary or ""
            lines.append(json.dumps({
                "문서id": row["doc_id"],
                "제목": row["title"] or row["filename"],
                "종류": row["doc_type"],
                "작성일": row["effective_date"] or "",
                "요약": summary[:200],
            }, ensure_ascii=False))
            chunks.append(_doc_chunk(doc, summary))
        return ToolResult(text=f"{len(docs)}건:\n" + "\n".join(lines),
                          chunks=chunks, note=f"목록 {len(docs)}건")

    # ── 도구 3. 내용 검색 ───────────────────────────────────────────────────
    def search_chunks(self, **cond) -> ToolResult:
        query = str(cond.get("검색어") or "").strip()
        if not query:
            return ToolResult(text="검색어가 필요합니다.", note="검색어 없음")
        top_k = min(int(cond.get("개수") or 8), MAX_SEARCH)
        # 종류·기간 같은 조건은 Qdrant payload 에 다 있지는 않다 → 검색 뒤 파이썬으로 거른다
        found = self.retrieve(query, self.policy, max(top_k * 3, 20))
        wanted = _doc_types(cond)
        if wanted:
            found = [c for c in found if c.payload.get("doc_type") in wanted]
        found = found[:top_k]
        if not found:
            return ToolResult(text="관련 내용을 찾지 못했습니다.", note=f"'{query}' 0건")

        lines = []
        for c in found:
            lines.append(json.dumps({
                "문서id": c.doc_id,
                "제목": c.title or c.source_filename,
                "쪽": c.page_no,
                "본문": (c.text or "")[:SNIPPET_CHARS],
            }, ensure_ascii=False))
        return ToolResult(text=f"{len(found)}개 조각:\n" + "\n".join(lines),
                          chunks=found, note=f"'{query}' {len(found)}건")

    # ── 도구 4. 문서 읽기 ───────────────────────────────────────────────────
    def read_document(self, **cond) -> ToolResult:
        doc_id = str(cond.get("문서id") or "").strip()
        if not doc_id:
            return ToolResult(text="문서id 가 필요합니다.", note="문서id 없음")

        from app.db.repositories import DocumentRepository
        repo = DocumentRepository(self.session)
        doc = repo.get(doc_id)
        # 권한 확인 — 여기가 뚫리면 LLM 이 아무 문서나 읽어 준다
        if doc is None or not self._readable(doc):
            return ToolResult(text="그런 문서가 없습니다.", note="문서 없음")

        body = "\n".join(c["text"] for c in repo.chunks_of(doc_id))[:MAX_DOC_CHARS]
        cls = doc.classification
        head = {"제목": cls.title_normalized or doc.identification.source_filename,
                "종류": cls.doc_type.value,
                "작성일": str(doc.lifecycle.effective_date or ""),
                "요약": cls.summary or ""}
        text = json.dumps(head, ensure_ascii=False) + "\n[본문]\n" + body
        return ToolResult(text=text, chunks=[_doc_chunk(doc, body or cls.summary or "")],
                          note=f"'{head['제목']}' 읽음")

    # ── 내부 ────────────────────────────────────────────────────────────────
    def _readable_rows(self, cond: dict[str, Any], limit: int) -> list:
        """조건에 맞고 **이 사용자가 실제로 볼 수 있는** 문서 [(row, doc), …].

        저장소 필터(`visible_to`)가 권한을 걸러 주지만 만료·대체까지는 안 본다.
        채팅 검색은 그것들을 빼므로, 목록·세기도 같은 규칙을 써야 답이 어긋나지 않는다.
        """
        from app.db.repositories import DocumentRepository
        from app.manage.service import DocumentManager

        rows = DocumentManager(self.session).list_documents(
            limit=max(limit * 3, limit + 20), sort="effective", desc=True,
            **self._filters(cond))
        repo = DocumentRepository(self.session)
        out = []
        for row in rows:
            doc = repo.get(row["doc_id"])
            if doc is None or not self._readable(doc):
                continue
            out.append((row, doc))
            if len(out) >= limit:
                break
        return out

    def _readable(self, doc) -> bool:
        """채팅 검색과 **똑같은 규칙**으로 판정한다(만료·대체·권한)."""
        return self.policy.allows(doc_level_payload(doc))

    def _filters(self, cond: dict[str, Any]) -> dict[str, Any]:
        """LLM 이 준 조건 → 저장소 필터. 권한은 여기서 **항상** 붙는다."""
        out: dict[str, Any] = {
            "visible_to": self.visibility,      # ← LLM 이 못 건드린다
            "indexed_only": True,
        }
        types = _doc_types(cond)
        if len(types) == 1:
            out["doc_type"] = next(iter(types))
        text = str(cond.get("검색어") or "").strip()
        if text:
            out["text"] = text
        if self.folder_node_ids:
            out["author_node_ids"] = set(self.folder_node_ids)
        return out


def _doc_types(cond: dict[str, Any]) -> set[str]:
    """'문서종류' 를 정식 어휘로 좁힌다 — 모르는 값은 버린다(거르지 않느니만 못하다)."""
    from app.schemas.enums import DocType
    known = {e.value for e in DocType}
    raw = cond.get("문서종류") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(v) for v in raw if str(v) in known}


def _doc_chunk(doc, text: str) -> RetrievedChunk:
    """문서 단위 근거 — 목록·읽기 결과도 인용할 수 있어야 출처가 붙는다."""
    ident = doc.identification
    payload = doc_level_payload(doc)
    payload["parent_doc_id"] = ident.doc_id
    payload["page_no"] = None
    return RetrievedChunk(chunk_id=f"{ident.doc_id}::doc", text=text or "",
                          score=0.0, payload=payload)
