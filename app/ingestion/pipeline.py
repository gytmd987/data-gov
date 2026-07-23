"""적재 파이프라인 오케스트레이션.

    UPLOADED → PARSED → AUTO_ENRICHED → PENDING_REVIEW → VALIDATED → INDEXED
                                             │
                                             └─(거버넌스 미충족)→ BLOCKED

- run_auto_stages(): intake→parse→chunk→enrich 까지 자동 수행 후 PENDING_REVIEW로 둔다.
- apply_review(): 사람이 입력한 거버넌스/보정 값을 병합하고 검증 → VALIDATED 또는 BLOCKED.
- index(): 검증된 문서의 청크를 임베딩·업서트하고 INDEXED로 전이.

외부 의존(임베딩·Qdrant·LLM·해시조회·OCR)은 모두 Protocol/콜백으로 주입 → 단위 테스트 가능.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Protocol

from app.governance.validator import ValidationResult, validate_governance
from app.schemas.enums import ChunkType
from app.schemas.ingestion import IngestionStatus, assert_transition
from app.schemas.metadata import (
    ClassificationBlock,
    DocumentMetadata,
    GovernanceBlock,
    LifecycleBlock,
    doc_level_payload,
)

from .chunking import Chunk, chunk_elements
from .enrichment import LLMClient, assert_ai_mandatory, enrich
from .intake import HashLookup, intake
from .parsers import build_parser_content, get_parser
from .parsers.base import ParseResult


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class Indexer(Protocol):
    def upsert(self, vectors: list[list[float]], payloads: list[dict[str, Any]],
               ids: list[str]) -> None: ...


OCRFn = Callable[[bytes], str]


@dataclass
class IngestionContext:
    doc: DocumentMetadata
    chunks: list[Chunk] = field(default_factory=list)
    status: IngestionStatus = IngestionStatus.UPLOADED
    parse_result: Optional[ParseResult] = None
    validation: Optional[ValidationResult] = None
    warnings: list[str] = field(default_factory=list)

    def _to(self, target: IngestionStatus) -> None:
        assert_transition(self.status, target)
        self.status = target


def run_auto_stages(
    path: str,
    ingested_by: str,
    llm: LLMClient,
    llm_model: str,
    ocr: Optional[OCRFn] = None,
    hash_lookup: Optional[HashLookup] = None,
) -> IngestionContext:
    """자동 단계(intake→parse→chunk→enrich). 완료 시 PENDING_REVIEW."""
    # UPLOADED: 식별 필드 + 중복 탐지
    ident = intake(path, ingested_by=ingested_by, hash_lookup=hash_lookup)
    doc = DocumentMetadata(identification=ident)
    ctx = IngestionContext(doc=doc, status=IngestionStatus.UPLOADED)

    # PARSED
    parser = get_parser(ident.file_format, ocr=ocr)
    result = parser.parse(path)
    ctx.parse_result = result
    ctx.doc.identification.page_count = result.page_count
    if not result.elements:
        ctx.warnings.append(
            "파싱 결과가 비어 있음(스캔 문서인데 OCR 미설정일 수 있음)."
        )
    ctx._to(IngestionStatus.PARSED)

    # 청킹
    ctx.chunks = chunk_elements(ident.doc_id, result.elements)

    # AUTO_ENRICHED: LLM 자동 채움(거버넌스 제외)
    content = build_parser_content(result)
    enrich(ctx.doc, content, client=llm, model_name=llm_model)
    # 요약·키워드·예상 Q&A 는 필수 — 못 채우면 파일 읽기 실패로 간주(ReadError)
    assert_ai_mandatory(ctx.doc)
    ctx._to(IngestionStatus.AUTO_ENRICHED)

    # 사람 검토 대기
    ctx._to(IngestionStatus.PENDING_REVIEW)
    return ctx


def apply_review(
    ctx: IngestionContext,
    governance: GovernanceBlock,
    classification_overrides: Optional[dict[str, Any]] = None,
    lifecycle_overrides: Optional[dict[str, Any]] = None,
    known_access_groups: Optional[Iterable[str]] = None,
    allowed_topics: Optional[Iterable[str]] = None,
) -> ValidationResult:
    """사람 입력 병합 + 검증. 통과 시 VALIDATED, 실패 시 BLOCKED."""
    if ctx.status not in (IngestionStatus.PENDING_REVIEW, IngestionStatus.BLOCKED):
        raise ValueError(f"검토 적용 불가 상태: {ctx.status.value}")

    # 사람이 확정한 거버넌스 필드로 교체(필수)
    ctx.doc.governance = governance

    # 분류/생애주기 사람 보정(선택). 문자열 입력도 enum으로 검증·강제되도록 model_validate 사용.
    if classification_overrides:
        data = ctx.doc.classification.model_dump()
        data.update(classification_overrides)
        ctx.doc.classification = ClassificationBlock.model_validate(data)
    if lifecycle_overrides:
        data = ctx.doc.lifecycle.model_dump()
        data.update(lifecycle_overrides)
        ctx.doc.lifecycle = LifecycleBlock.model_validate(data)

    result = validate_governance(
        ctx.doc,
        known_access_groups=known_access_groups,
        allowed_topics=allowed_topics,
    )
    ctx.validation = result

    if ctx.status == IngestionStatus.BLOCKED:
        # 재검토: BLOCKED → PENDING_REVIEW 후 판정
        ctx._to(IngestionStatus.PENDING_REVIEW)
    ctx._to(result.as_status())  # VALIDATED or BLOCKED
    return result


def index(ctx: IngestionContext, embedder: Embedder, indexer: Indexer) -> int:
    """검증된 문서의 청크를 임베딩·업서트하고 INDEXED로 전이. 반환: 색인된 청크 수."""
    if ctx.status != IngestionStatus.VALIDATED:
        raise ValueError(
            f"색인 불가: 검증되지 않은 문서(status={ctx.status.value})")
    if not ctx.chunks:
        ctx._to(IngestionStatus.INDEXED)
        return 0

    texts = [c.text for c in ctx.chunks]
    payloads = []
    for c in ctx.chunks:
        pl = c.meta.to_qdrant_payload(ctx.doc)
        pl["text"] = c.text   # 검색 결과·리랭킹·답변에 원문 필요
        payloads.append(pl)
    ids = [c.meta.chunk_id for c in ctx.chunks]

    # 합성 Q&A 청크: 요약+키워드+예상 Q&A 를 별도 포인트로 색인해 질문형 질의의
    # recall 을 높인다. 원문 청크 벡터는 건드리지 않아 near-dup·리랭킹에 영향 없음.
    synth = build_qa_chunk_text(ctx.doc)
    if synth:
        doc_id = ctx.doc.identification.doc_id
        qa_pl = doc_level_payload(ctx.doc)
        qa_pl.update({"chunk_id": f"{doc_id}::qa", "parent_doc_id": doc_id,
                      "chunk_type": ChunkType.QA.value, "section_title": None,
                      "page_no": None, "text": synth})
        texts.append(synth)
        payloads.append(qa_pl)
        ids.append(f"{doc_id}::qa")

    vectors = embedder.embed(texts)
    indexer.upsert(vectors=vectors, payloads=payloads, ids=ids)

    ctx._to(IngestionStatus.INDEXED)
    return len(ctx.chunks)   # 콘텐츠 청크 수(합성 청크 제외)


def build_qa_chunk_text(doc) -> str:
    """합성 Q&A 청크 텍스트 = 요약 + 핵심 키워드 + 예상 Q&A. 없으면 빈 문자열."""
    cls = doc.classification
    parts: list[str] = []
    if cls.summary:
        parts.append(cls.summary)
    if cls.keywords:
        parts.append("핵심 키워드: " + ", ".join(cls.keywords))
    for qa in cls.expected_qa:
        q = (qa.get("question") or "").strip()
        a = (qa.get("answer") or "").strip()
        if q:
            parts.append(f"Q. {q}\nA. {a}")
    return "\n".join(parts).strip()
