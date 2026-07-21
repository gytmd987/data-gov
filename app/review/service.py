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
    DocType,
    Language,
    PiiType,
    SensitivityLevel,
)
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import GovernanceBlock

OCRFn = Callable[[bytes], str]


# 폼 드롭다운용 enum 옵션
ENUM_OPTIONS: dict[str, list[str]] = {
    "doc_type": [e.value for e in DocType],
    "language": [e.value for e in Language],
    "sensitivity_level": [e.value for e in SensitivityLevel],
    "pii_types": [e.value for e in PiiType],
    "lifecycle_status": [e.value for e in DocStatus],
}


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
    last_validation: Optional[dict[str, Any]] = None


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
    def start_ingestion(self, path: str, ingested_by: str) -> str:
        repo = self.docs
        ctx = run_auto_stages(
            path, ingested_by=ingested_by,
            llm=self.llm, llm_model=self.llm_model, ocr=self.ocr,
            hash_lookup=persistent_hash_lookup(repo),
        )
        save_ingestion(repo, ctx)
        # 원본 파일 보관(열람/다운로드용)
        self._store_original(path, ctx.doc.identification.doc_id,
                             ctx.doc.identification.file_format.value)
        # 유사(개정판 가능) 문서 자동 탐지 → 검토 화면에서 사람이 판단
        self._detect_similar(ctx)
        self.session.commit()
        return ctx.doc.identification.doc_id

    def _store_original(self, src_path: str, doc_id: str, ext: str) -> None:
        try:
            import shutil
            from pathlib import Path
            from app.config import settings
            dest_dir = Path(settings.storage_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = (dest_dir / f"{doc_id}.{ext}").resolve()   # 절대경로로 보관
            shutil.copyfile(src_path, dest)
            self.docs.set_original_path(doc_id, str(dest))
        except Exception:
            pass  # 원본 보관 실패해도 적재는 계속(열람만 불가)

    def _detect_similar(self, ctx) -> None:
        client = getattr(self.indexer, "client", None)
        collection = getattr(self.indexer, "collection", None)
        if client is None or not ctx.chunks:
            return
        try:
            from app.ingestion.dedup import find_similar
            text = "\n".join(c.text for c in ctx.chunks[:20])
            cands = find_similar(client, collection, self.embedder, text,
                                 exclude_doc_id=ctx.doc.identification.doc_id)
            if cands:
                self.docs.set_similar_candidates(ctx.doc.identification.doc_id, cands)
        except Exception:
            pass  # 탐지는 부가 기능 — 실패해도 적재는 계속

    # ── 검토 대기 목록 ───────────────────────────────────────────────────────
    def list_pending(self) -> list[dict[str, Any]]:
        repo = self.docs
        ids = (repo.list_by_status(IngestionStatus.PENDING_REVIEW)
               + repo.list_by_status(IngestionStatus.BLOCKED))
        out = []
        for doc_id in ids:
            doc = repo.get(doc_id)
            if doc is None:
                continue
            out.append({
                "doc_id": doc_id,
                "filename": doc.identification.source_filename,
                "doc_type": doc.classification.doc_type.value,
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
                "department": cls.department,
                "team": cls.team,
                "topics": cls.topics,
                "language": cls.language.value,
            },
            lifecycle={
                "lifecycle_status": life.status.value,
                "effective_date": life.effective_date.isoformat() if life.effective_date else None,
                "expiry_date": life.expiry_date.isoformat() if life.expiry_date else None,
                "version": life.version,
            },
            governance={
                "sensitivity_level": gov.sensitivity_level.value if gov.sensitivity_level else None,
                "contains_pii": gov.contains_pii,
                "pii_types": [p.value for p in gov.pii_types],
                "access_groups": gov.access_groups,
                "owner": gov.owner,
            },
            known_groups=self._known_groups(),
            similar_candidates=repo.get_similar_candidates(doc_id),
        )

    def _known_groups(self) -> list[str]:
        """선택/검증 가능한 접근 그룹 = config 어휘 ∪ DB에 실재하는 그룹."""
        return sorted(set(system_config.access_groups())
                      | set(self.users.known_access_groups()))

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

        result = apply_review(
            ctx, governance=governance,
            classification_overrides=classification_overrides,
            lifecycle_overrides=lifecycle_overrides,
            known_access_groups=self._known_groups(),
            allowed_topics=None,
        )
        save_ingestion(repo, ctx)

        if result.ok and do_index:
            index(ctx, embedder=self.embedder, indexer=self.indexer)
            save_ingestion(repo, ctx)

        self.session.commit()
        return result
