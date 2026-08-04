"""검토(human-in-the-loop) 서비스 계층 — UI 비의존.

Streamlit 앱은 이 서비스만 호출한다(로직/테스트를 UI에서 분리).
흐름: start_ingestion(자동단계+저장) → list_pending → get_review → submit_review(검증·차단·색인).

외부 서비스(LLM/Embedder/Indexer/OCR)는 생성자 주입 → 테스트에서 fake 사용.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app import system_config
from app.db.persistence import load_context, persistent_hash_lookup, save_ingestion
from app.db.repositories import DocumentRepository, UserRepository
from app.governance.validator import ValidationResult
from app.ingestion.enrichment import LLMClient
from app.ingestion.pipeline import (
    Embedder,
    Indexer,
    apply_review,
    index,
    run_auto_stages,
)
from app.schemas.enums import (
    DocStatus,
    Language,
)
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import GovernanceBlock

OCRFn = Callable[[bytes], str]


# 사용자에게 노출하는 생애주기 상태(내부 draft/superseded 는 숨김).
# 대부분 문서가 '보관'이라 보관을 앞에 두고 등록 기본값으로 쓴다.
USER_DOC_STATUSES: list[str] = [
    DocStatus.ARCHIVED.value, DocStatus.ACTIVE.value, DocStatus.EXPIRED.value]


def _doc_type_options() -> list[str]:
    """문서 종류 드롭다운 — '미분류(unknown)' 제외, 라벨 가나다순, '기타(other)'는 맨 끝."""
    vals = [v for v in system_config.doc_types() if v not in ("unknown", "other")]
    vals.sort(key=lambda v: system_config.label(v))
    if "other" in system_config.doc_types():
        vals.append("other")
    return vals


# 폼 드롭다운용 enum 옵션 — 문서 종류의 'unknown'(미분류)은 선택지에서 제외한다.
ENUM_OPTIONS: dict[str, list[str]] = {
    "doc_type": _doc_type_options(),
    "language": [e.value for e in Language],
    "lifecycle_status": USER_DOC_STATUSES,
}


def _expand_access_tokens(session, governance: GovernanceBlock) -> GovernanceBlock:
    """access_selections(node:/head:) → 조직 트리로 확장한 access_tokens 를 채운다."""
    from app.db.repositories import OrgRepository
    tree = OrgRepository(session).load_tree()
    return governance.model_copy(update={
        "access_tokens": tree.readable_tokens(governance.access_selections)})


@dataclass
class ReviewView:
    """검토 화면에 필요한 데이터 묶음."""

    doc_id: str
    source_filename: str
    file_format: str
    status: str
    page_count: Optional[int]
    chunk_count: int
    # LLM 자동 채움(값 + confidence)
    auto_filled: list[dict[str, Any]] = field(default_factory=list)
    classification: dict[str, Any] = field(default_factory=dict)
    lifecycle: dict[str, Any] = field(default_factory=dict)
    governance: dict[str, Any] = field(default_factory=dict)
    known_groups: list[str] = field(default_factory=list)
    similar_candidates: list[dict[str, Any]] = field(default_factory=list)
    revision_candidates: list[dict[str, Any]] = field(default_factory=list)  # 개정판(교체) 후보
    related_recos: list[dict[str, Any]] = field(default_factory=list)        # AI 추천 연관 문서
    last_validation: Optional[dict[str, Any]] = None
    filename_stem: Optional[str] = None   # 제목 보정 고지용(제목≠파일명이면 배너)


@dataclass
class ReviewService:
    session: Session
    llm: LLMClient
    llm_model: str
    embedder: Embedder
    indexer: Indexer
    ocr: Optional[OCRFn] = None

    # ── 리포지토리 ────────────────────────────────────────────────────────
    @property
    def docs(self) -> DocumentRepository:
        return DocumentRepository(self.session)

    @property
    def users(self) -> UserRepository:
        return UserRepository(self.session)

    # ── 적재 시작(자동 단계) ────────────────────────────────────────────────
    def start_ingestion(self, path: str, ingested_by: str,
                        folder_node_id: Optional[int] = None) -> str:
        """업로드 → 자동 채움 → 검토 대기.

        folder_node_id(폴더=조직노드)를 주면 작성부서·접근권한·저장경로가 그 폴더 기준으로
        세팅된다. 없으면 업로더의 대표 소속 노드로 폴백한다.
        """
        repo = self.docs
        ctx = run_auto_stages(
            path, ingested_by=ingested_by,
            llm=self.llm, llm_model=self.llm_model, ocr=self.ocr,
            hash_lookup=persistent_hash_lookup(repo),
        )
        # 제목 = 파일명 기반 + 날짜 정규화(YY-MMDD 앞쪽). 파일명이 의미없으면 AI 제안 사용.
        from app.ingestion.titletools import compose_title
        ctx.doc.classification.title_normalized = compose_title(
            ctx.doc.identification.source_filename,
            ai_title=ctx.doc.classification.title_normalized,
            ai_date=ctx.doc.lifecycle.effective_date)
        # 등록 기본 상태 = 보관(대부분 문서가 보관이며 보관도 검색에 노출됨)
        ctx.doc.lifecycle.status = DocStatus.ARCHIVED

        # 작성자 기본값 = 업로더 + 그의 대표 소속 노드(부서장 관리 범위 판정)
        author = self.users.get_user(ingested_by)
        ctx.doc.governance.author_id = ingested_by
        if author is not None:
            ctx.doc.governance.author_name = author.display_name
        # 작성부서·권한 기본값 = 업로드할 때 고른 '폴더'(조직노드) 기준.
        # 폴더를 안 골랐으면 업로더의 대표 소속 노드로 폴백.
        node_id = folder_node_id or self.users.primary_node(ingested_by)
        ctx.doc.governance.author_node_id = node_id
        if node_id is not None:
            from app.db.repositories import OrgRepository
            org = OrgRepository(self.session)
            node = org.get(node_id)
            if node is not None:
                ctx.doc.classification.department = node.name   # 작성부서(표시용)
            # 열람 권한 기본값 = 폴더에 설정된 값(없으면 그 폴더가 속한 부서 전체).
            # **기본값일 뿐** — 검토 화면에서 사람이 바꿀 수 있다.
            ctx.doc.governance.access_selections = org.default_access_for(node_id)
        save_ingestion(repo, ctx)
        # 원본 파일 보관(폴더=조직노드 경로에 저장)
        self._store_original(path, ctx.doc.identification.doc_id,
                             ctx.doc.identification.file_format.value,
                             node_id=node_id,
                             title=ctx.doc.classification.title_normalized)
        # 유사(개정판 가능) 문서 자동 탐지 → 검토 화면에서 사람이 판단
        self._detect_similar(ctx)
        self.session.commit()
        return ctx.doc.identification.doc_id

    def finalize_original_name(self, doc_id: str) -> None:
        """등록 확정 후, 원본을 '폴더(작성부서) 경로 + 제목' 위치로 정리한다."""
        from pathlib import Path
        from app.manage.storage import place
        doc = self.docs.get(doc_id)
        path = self.docs.get_original_path(doc_id)
        if doc is None or not path:
            return
        ext = Path(path).suffix or f".{doc.identification.file_format.value}"
        title = doc.classification.title_normalized or Path(path).stem
        place(self.session, doc_id, doc.governance.author_node_id, title, ext)

    def _store_original(self, src_path: str, doc_id: str, ext: str,
                        node_id: Optional[int] = None,
                        title: Optional[str] = None) -> None:
        """원본을 폴더(조직노드) 경로에 보관. 폴더가 없으면 `_미분류`."""
        try:
            import shutil
            from pathlib import Path
            from app.db.repositories import OrgRepository
            from app.manage.storage import target_path
            tree = OrgRepository(self.session).load_tree()
            stem = title or Path(src_path).stem
            dest = target_path(tree, node_id, stem, ext, doc_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest = dest.with_name(f"{dest.stem}_{doc_id[:6]}{dest.suffix}")
            shutil.copyfile(src_path, dest)
            self.docs.set_original_path(doc_id, str(dest.resolve()))
        except Exception:
            pass  # 원본 보관 실패해도 적재는 계속(열람만 불가)

    def _detect_similar(self, ctx) -> None:
        client = getattr(self.indexer, "client", None)
        collection = getattr(self.indexer, "collection", None)
        if client is None or not ctx.chunks:
            return
        try:
            from app.ingestion.dedup import find_similar
            from app.relations.detect import DUP_THRESHOLD, RELATED_FLOOR
            doc_id = ctx.doc.identification.doc_id
            text = "\n".join(c.text for c in ctx.chunks[:20])
            # 한 번의 검색으로 개정판(>=0.88)과 연관 후보(중간대)를 함께 얻는다.
            cands = find_similar(client, collection, self.embedder, text,
                                 exclude_doc_id=doc_id, threshold=RELATED_FLOOR)
            if cands:
                dup = [c for c in cands if c["score"] >= DUP_THRESHOLD]
                if dup:
                    self._classify_candidates(ctx.doc, dup)   # AI 관계 제안 부착
                # 개정판(dup) + 연관 추천(중간대)을 함께 보관 → 검토 화면에서 활용
                self.docs.set_similar_candidates(doc_id, cands[:8])
            self._auto_link_relations(ctx, sim_candidates=cands)
        except Exception:
            pass  # 탐지는 부가 기능 — 실패해도 적재는 계속

    def _classify_candidates(self, new_doc, candidates) -> None:
        """유사(개정판 대역) 후보마다 AI 관계 제안(revision/related/unrelated)을 부착."""
        from app.relations.classify import classify_relation
        nt = new_doc.classification.title_normalized
        ns = new_doc.classification.summary
        for c in candidates[:3]:
            cand = self.docs.get(c["doc_id"])
            if cand is None:
                continue
            res = classify_relation(self.llm, nt, ns,
                                    cand.classification.title_normalized,
                                    cand.classification.summary)
            c["ai_relation"] = res["relation"]
            c["ai_reason"] = res["reason"]

    def _auto_link_relations(self, ctx, sim_candidates=None) -> None:
        """업로드 시 연관 문서를 자동 감지해 연결(신호 1개만 잡혀도 연결)."""
        from app.db.repositories import RelationRepository
        from app.relations.detect import detect_related
        doc = ctx.doc
        doc_id = doc.identification.doc_id
        existing = self.docs.list_documents(limit=500)   # 최근 문서와 비교
        found = detect_related(
            this_doc_id=doc_id,
            this_filename=doc.identification.source_filename,
            mentions=doc.classification.references,
            existing=existing,
            sim_candidates=sim_candidates,
        )
        rel = RelationRepository(self.session)
        for r in found:
            rel.link(doc_id, r["doc_id"], source="auto",
                     confidence=r["confidence"], reason=r["reason"])

    # ── 검토 대기 목록 ───────────────────────────────────────────────────────
    def list_pending(self, author_id: Optional[str] = None) -> list[dict[str, Any]]:
        """검토 대기(+차단) 문서. author_id 지정 시 그 사람이 올린 문서만."""
        repo = self.docs
        ids = (repo.list_by_status(IngestionStatus.PENDING_REVIEW)
               + repo.list_by_status(IngestionStatus.BLOCKED))
        out = []
        for doc_id in ids:
            doc = repo.get(doc_id)
            if doc is None:
                continue
            if author_id is not None and doc.governance.author_id != author_id:
                continue
            out.append({
                "doc_id": doc_id,
                "filename": doc.identification.source_filename,
                "doc_type": doc.classification.doc_type.value,
                "title": doc.classification.title_normalized,
                "status": self._status_of(doc_id),
            })
        return out

    def _status_of(self, doc_id: str) -> str:
        from app.db.models import Document
        row = self.session.get(Document, doc_id)
        return row.status if row else "unknown"

    # ── 검토 상세 ────────────────────────────────────────────────────────────
    def get_review(self, doc_id: str) -> Optional[ReviewView]:
        repo = self.docs
        doc = repo.get(doc_id)
        if doc is None:
            return None
        ctx = load_context(repo, doc_id)
        chunk_count = len(ctx.chunks) if ctx else 0
        cls = doc.classification
        life = doc.lifecycle
        gov = doc.governance
        return ReviewView(
            doc_id=doc_id,
            source_filename=doc.identification.source_filename,
            file_format=doc.identification.file_format.value,
            status=self._status_of(doc_id),
            page_count=doc.identification.page_count,
            chunk_count=chunk_count,
            auto_filled=[a.model_dump() for a in doc.provenance.auto_filled],
            classification={
                "doc_type": cls.doc_type.value,
                "title_normalized": cls.title_normalized,
                "summary": cls.summary,
                "keywords": cls.keywords,
                "expected_qa": cls.expected_qa,
                "related_parties": cls.related_parties,
                "department": cls.department,
                "language": cls.language.value,
            },
            lifecycle={
                "lifecycle_status": life.status.value,
                "effective_date": life.effective_date.isoformat() if life.effective_date else None,
                "expiry_date": life.expiry_date.isoformat() if life.expiry_date else None,
                "version": life.version,
            },
            governance={
                "access_selections": gov.access_selections,
                "author_id": gov.author_id,
                "author_name": gov.author_name,
                "author_node_id": gov.author_node_id,
                "reporting_line": gov.reporting_line,
            },
            similar_candidates=repo.get_similar_candidates(doc_id),
        )

    # ── 검토 제출(검증·차단·색인) ────────────────────────────────────────────
    def submit_review(
        self,
        doc_id: str,
        governance: GovernanceBlock,
        classification_overrides: Optional[dict[str, Any]] = None,
        lifecycle_overrides: Optional[dict[str, Any]] = None,
        do_index: bool = True,
    ) -> ValidationResult:
        repo = self.docs
        ctx = load_context(repo, doc_id)
        if ctx is None:
            raise ValueError(f"문서 없음: {doc_id}")

        governance = _expand_access_tokens(self.session, governance)
        if governance.author_node_id is None:
            governance = governance.model_copy(update={
                "author_node_id": ctx.doc.governance.author_node_id})
        result = apply_review(
            ctx, governance=governance,
            classification_overrides=classification_overrides,
            lifecycle_overrides=lifecycle_overrides,
        )
        save_ingestion(repo, ctx)

        if result.ok and do_index:
            index(ctx, embedder=self.embedder, indexer=self.indexer)
            save_ingestion(repo, ctx)

        self.session.commit()
        return result
