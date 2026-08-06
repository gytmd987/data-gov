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
from app.db.persistence import (
    load_context,
    persistent_hash_lookup,
    persistent_message_id_lookup,
    save_ingestion,
)
from app.db.repositories import DocumentRepository, UserRepository
from app.governance.validator import ValidationResult
from app.ingestion.enrichment import DEFAULT_CONFIDENCE_THRESHOLD, LLMClient
from app.ingestion.intake import ext_for
from app.ingestion.pipeline import (
    Embedder,
    Indexer,
    apply_review,
    index,
    run_auto_stages,
)
from app.schemas.enums import (
    DocStatus,
    FileFormat,
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


# 내부 필드 이름 → 화면에 쓸 한글 이름. 사용자에게 'title_normalized' 같은 건 보이면 안 된다.
FIELD_LABELS = {
    "doc_type": "문서 종류",
    "title_normalized": "제목",
    "summary": "요약",
    "department": "작성부서",
    "language": "언어",
    "status": "상태",
    "effective_date": "작성일",
    "expiry_date": "유효일",
    "version": "버전",
}


def uncertain_field_labels(auto_filled, threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
                           ) -> list[str]:
    """AI가 확신하지 못한 항목의 **한글 이름** 목록(검토 화면에서 주의를 끌기 위함).

    신뢰도 숫자나 내부 필드명을 그대로 보여주면 사용자에게 의미가 없다.
    """
    out: list[str] = []
    for item in auto_filled or []:
        data = item if isinstance(item, dict) else item.model_dump()
        if float(data.get("confidence", 1.0)) < threshold:
            label = FIELD_LABELS.get(data.get("field"))
            if label and label not in out:
                out.append(label)
    return out


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
    uncertain_fields: list[str] = field(default_factory=list)   # AI가 확신 못 한 항목(한글)
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
                        folder_node_id: Optional[int] = None,
                        _depth: int = 0) -> str:
        """업로드 → 자동 채움 → 검토 대기.

        folder_node_id(폴더=조직노드)를 주면 작성부서·접근권한·저장경로가 그 폴더 기준으로
        세팅된다. 없으면 업로더의 대표 소속 노드로 폴백한다.

        _depth 는 메일 첨부를 다시 적재할 때만 쓰는 내부 인자다(무한 재귀 방지).
        """
        repo = self.docs
        ctx = run_auto_stages(
            path, ingested_by=ingested_by,
            llm=self.llm, llm_model=self.llm_model, ocr=self.ocr,
            hash_lookup=persistent_hash_lookup(repo),
            message_id_lookup=persistent_message_id_lookup(repo),
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

        # 메일이면 첨부를 꺼내 별도 문서로 등록하고, 보관하는 메일에서는 덜어낸다.
        # (첨부는 본문에 base64 로 붙어 있어 용량만 먹고 검색에는 전혀 안 잡혔다)
        doc_id = ctx.doc.identification.doc_id
        store_path = ctx.source_path or path
        if ctx.doc.identification.file_format is FileFormat.EMAIL:
            store_path = self._split_mail_attachments(
                store_path, doc_id, ingested_by=ingested_by,
                folder_node_id=node_id, depth=_depth)

        if ctx.doc.identification.file_format is FileFormat.EMAIL:
            self._link_mail_thread(ctx.doc)

        # 원본 파일 보관(폴더=조직노드 경로에 저장). 확장자는 형식에 맞는 실제 확장자로.
        self._store_original(store_path, doc_id,
                             ext_for(ctx.doc.identification.file_format),
                             node_id=node_id,
                             title=ctx.doc.classification.title_normalized)
        # 유사(개정판 가능) 문서 자동 탐지 → 검토 화면에서 사람이 판단
        self._detect_similar(ctx)
        self.session.commit()
        return doc_id

    # 첨부 안에 든 메일까지 다시 풀지는 않는다(스레드가 통째로 딸려 오면 끝없이 번진다)
    MAIL_DEPTH_LIMIT = 1

    def _split_mail_attachments(self, mail_path: str, doc_id: str, *,
                                ingested_by: str, folder_node_id: Optional[int],
                                depth: int) -> str:
        """메일 첨부를 별도 문서로 등록하고, 첨부를 덜어낸 메일 파일 경로를 돌려준다.

        - 등록에 성공했거나 **이미 등록돼 있던**(같은 첨부가 스레드마다 따라온 경우)
          첨부만 메일에서 덜어낸다. 그래야 원본이 사라지는 일이 없다.
        - 첨부 문서의 폴더·권한은 메일과 같다(같은 자리에서 온 파일이므로).
        - 등록한 첨부는 메일과 '연관'으로 연결해 문서 상세에서 서로 오갈 수 있게 한다.
        """
        import tempfile
        from pathlib import Path

        from app.db.repositories import RelationRepository
        from app.ingestion.intake import SUPPORTED_SUFFIXES, DuplicateError
        from app.ingestion.mailfile import rebuild_without, split_attachments

        if depth >= self.MAIL_DEPTH_LIMIT:
            return mail_path
        try:
            _, attachments = split_attachments(mail_path)
        except Exception:                       # noqa: BLE001 — 첨부 분리 실패는 무시
            return mail_path
        if not attachments:
            return mail_path

        rel = RelationRepository(self.session)
        work = Path(tempfile.mkdtemp(prefix="attach_"))
        detached: list[str] = []
        for att in attachments:
            if att.suffix not in SUPPORTED_SUFFIXES:
                continue                        # 못 읽는 형식은 메일 안에 그대로 둔다
            tmp = work / att.filename
            try:
                tmp.write_bytes(att.data)
            except OSError:
                continue
            try:
                child = self.start_ingestion(str(tmp), ingested_by=ingested_by,
                                             folder_node_id=folder_node_id,
                                             _depth=depth + 1)
            except DuplicateError as e:
                # 같은 첨부가 다른 메일로 이미 들어와 있다 — 그 문서에 연결만 하고 덜어낸다
                child = e.existing_doc_id
            except Exception:                   # noqa: BLE001 — 이 첨부만 포기
                continue
            if child:
                rel.link(doc_id, child, source="auto", reason="메일 첨부")
                detached.append(att.filename)

        if not detached:
            return mail_path
        try:
            slim = work / f"{Path(mail_path).stem}.eml"
            slim.write_bytes(rebuild_without(mail_path, detached))
            return str(slim)
        except Exception:                       # noqa: BLE001 — 실패하면 원본 그대로 보관
            return mail_path

    def confirm_without_review(self, doc_id: str) -> None:
        """사람 검토 없이 등록 확정 — 대량 반입·예약 업로드가 함께 쓴다.

        권한·작성부서는 AI 가 아니라 **폴더에서** 오므로 검토를 건너뛰어도 안전하다.
        검색 품질에 영향을 주는 분류 항목만 AI 값이며 등록 후 수정할 수 있다.
        AI 가 종류를 정하지 못했으면 '미분류'로 남기지 않고 '기타'로 둔다.
        """
        doc = self.docs.get(doc_id)
        if doc is None:
            raise ValueError(f"문서 없음: {doc_id}")
        overrides = {}
        dt = doc.classification.doc_type
        if dt is None or dt.value == "unknown":
            overrides["doc_type"] = "other"

        result = self.submit_review(doc_id, governance=doc.governance,
                                    classification_overrides=overrides or None)
        if not result.ok:
            raise RuntimeError(", ".join(result.missing_fields + result.errors))

        self.finalize_original_name(doc_id)      # 서버 파일명을 제목으로 정리
        try:                                     # 표 데이터면 DuckDB 구조화 적재
            from app.datasets.loader import ingest_if_tabular
            ingest_if_tabular(self.session, self.docs.get(doc_id))
        except Exception:                        # noqa: BLE001 — 등록 자체는 유지
            pass
        self.session.commit()
        # 메일에서 떼어낸 첨부도 같이 등록한다. 안 그러면 메일만 검색되고 첨부는
        # 검토 대기에 남아, 정작 찾으려던 내용이 안 잡힌다.
        self._confirm_mail_attachments(doc_id)

    def _link_mail_thread(self, doc) -> None:
        """같은 스레드의 메일끼리 연결한다 — **헤더 기준**이라 정확하다.

        답장·전달 관계는 본문 인용문이 아니라 In-Reply-To / References 헤더에 있다.
        그래서 본문에서 인용문을 걷어내도 관계는 그대로 남는다. 유사도로 추측하던
        것과 달리 여기서 나오는 연결은 메일 프로그램이 기록한 사실이다.

        스레드 뿌리가 같으면 올린 순서나 중간 메일 누락과 무관하게 묶인다.

        References 를 안 채워 보내는 전달 메일은 여기서 안 잡히지만, 내용이 원본과
        거의 같으므로 기존 유사도 감지(_auto_link_relations)가 대신 잡는다.
        """
        from app.db.repositories import RelationRepository

        ident = doc.identification
        rel = RelationRepository(self.session)
        doc_id, mine = ident.doc_id, ident.message_id

        for other in self.docs.thread_members(ident.thread_root, exclude_doc_id=doc_id):
            # 직접 답장 관계면 그렇게 표시하고, 아니면 같은 스레드로만 묶는다
            direct = (mine and other.get("in_reply_to") == mine) or (
                ident.in_reply_to and ident.in_reply_to == other.get("message_id"))
            rel.link(doc_id, other["doc_id"], source="auto", confidence=1.0,
                     reason="메일 답장" if direct else "메일 스레드")

    def _confirm_mail_attachments(self, doc_id: str) -> None:
        """이 메일에서 떼어낸 첨부 중 아직 검토 대기인 것을 함께 등록한다."""
        from app.db.repositories import RelationRepository
        pending = {IngestionStatus.PENDING_REVIEW.value, IngestionStatus.BLOCKED.value}
        for link in RelationRepository(self.session).related_ids(doc_id):
            if link.get("reason") != "메일 첨부":
                continue
            child = link["doc_id"]
            if self.docs.get_status(child) not in pending:
                continue
            try:
                self.confirm_without_review(child)
            except Exception:                    # noqa: BLE001 — 메일 등록은 유지
                self.session.rollback()

    def finalize_original_name(self, doc_id: str) -> None:
        """등록 확정 후, 원본을 '폴더(작성부서) 경로 + 제목' 위치로 정리한다."""
        from pathlib import Path
        from app.manage.storage import place
        doc = self.docs.get(doc_id)
        path = self.docs.get_original_path(doc_id)
        if doc is None or not path:
            return
        ext = Path(path).suffix or ext_for(doc.identification.file_format)
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
            uncertain_fields=uncertain_field_labels(doc.provenance.auto_filled),
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
