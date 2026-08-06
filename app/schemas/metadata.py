"""Controlled 메타데이터 스키마.

블록 구성(초안 반영):
  - 식별·기술 (시스템 자동)
  - 내용 분류 (LLM 추론 → 사람 확인)
  - 거버넌스·접근통제 (사람 필수 확인)
  - 생애주기 (LLM 추론 + 사람 확인)
  - 출처 (혼합)
  - 청크 레벨

핵심: 자유 텍스트 분류 필드 금지. 분류/거버넌스/생애주기 값은 enums.py의 controlled vocabulary만 허용한다.
LLM이 채운 필드는 auto_filled에 신뢰도와 함께 기록한다.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from .enums import (
    ChunkType,
    DocStatus,
    DocType,
    FileFormat,
    Language,
)


def _coerce_enum(enum_cls, fallback: str):
    """저장된 값이 현재 어휘에 없으면 fallback 으로 대체하는 before-validator.

    설정(config)에서 문서 종류 등을 나중에 바꿔도 기존 문서 로딩이 깨지지 않게 한다.
    (예: 예전에 'policy'로 저장됐는데 지금 어휘에 없으면 'unknown' 등으로 안전 대체.)
    """
    def _validate(value):
        if value is None:
            return value
        raw = getattr(value, "value", value)
        return raw if raw in {e.value for e in enum_cls} else fallback
    return _validate


class AutoFilledField(BaseModel):
    """LLM이 자동으로 채운 필드 1건의 출처·신뢰도 기록."""

    field: str
    confidence: float = Field(ge=0.0, le=1.0)
    model: str
    source_span: Optional[str] = None  # 근거가 된 문서 내 위치/발췌(선택)


# ── 블록 1. 식별·기술 (시스템 자동) ──────────────────────────────────────────
class IdentificationBlock(BaseModel):
    doc_id: str
    source_filename: str
    file_format: FileFormat
    file_hash: str                      # 중복 탐지 (sha256 등)
    # 메일 고유값. 같은 메일을 사서함마다 저장하면 헤더가 달라 해시가 어긋나므로,
    # 메일은 이 값으로 중복을 판정한다(메일이 아니면 None).
    message_id: Optional[str] = None
    # 스레드 — 답장·전달 관계는 본문 인용문이 아니라 헤더에 들어 있다.
    in_reply_to: Optional[str] = None     # 바로 위 메일(무엇에 대한 답장인가)
    thread_root: Optional[str] = None     # 스레드의 첫 메일(같으면 한 스레드)
    ingested_at: datetime
    ingested_by: str
    page_count: Optional[int] = None
    last_modified: Optional[datetime] = None   # 문서 최종 수정일(파일 속성)

    _v_fmt = field_validator("file_format", mode="before")(
        _coerce_enum(FileFormat, "other"))


# ── 블록 2. 내용 분류 (LLM 추론 → 사람 확인) ────────────────────────────────
class ClassificationBlock(BaseModel):
    doc_type: DocType = DocType.UNKNOWN
    title_normalized: Optional[str] = None
    summary: Optional[str] = None                        # [AI 필수]
    keywords: list[str] = Field(default_factory=list)    # [AI 필수] 핵심 키워드(Q&A와 중복 금지)
    expected_qa: list[dict] = Field(default_factory=list)  # [AI 필수] [{question, answer}, ...]
    related_parties: list[str] = Field(default_factory=list)  # 유관 조직/임직원(AI 제안)
    references: list[str] = Field(default_factory=list)   # 본문이 언급한 다른 문서(제목/파일명) — 연관 자동감지용
    department: Optional[str] = None                     # 작성 부서(조직 노드 이름)
    language: Language = Language.UNKNOWN

    # 어휘 변경(예: 예전 'policy' 제거)에도 기존 문서 로딩이 깨지지 않게 안전 대체
    _v_type = field_validator("doc_type", mode="before")(
        _coerce_enum(DocType, "unknown"))
    _v_lang = field_validator("language", mode="before")(
        _coerce_enum(Language, "unknown"))


# ── 블록 3. 접근통제 (조직도 기반) ──────────────────────────────────────────
class GovernanceBlock(BaseModel):
    # access_selections: 사람이 고른 접근 대상. 항목은 "node:<id>"(부서 전체) 또는
    #   "head:<id>"(부서장만). 빈 값이면 팀 전체 공개. access_tokens 는 그 확장 결과(payload용).
    access_selections: list[str] = Field(default_factory=list)
    access_tokens: list[str] = Field(default_factory=list)
    author_id: Optional[str] = None                      # 작성자 ID(기본값 현재 유저)
    author_name: Optional[str] = None                    # 작성자 이름
    author_node_id: Optional[int] = None                 # 작성자 소속 조직 노드(부서장 관리 범위 판정)
    reporting_line: list[str] = Field(default_factory=list)  # 보고선(조직도 상위 라인)


# ── 블록 4. 생애주기 (LLM 추론 + 사람 확인) ─────────────────────────────────
class LifecycleBlock(BaseModel):
    effective_date: Optional[date] = None
    review_date: Optional[date] = None
    expiry_date: Optional[date] = None
    version: Optional[str] = None
    status: DocStatus = DocStatus.DRAFT
    supersedes: Optional[str] = None       # 이 문서가 대체하는 doc_id
    superseded_by: Optional[str] = None     # 이 문서를 대체한 doc_id

    _v_status = field_validator("status", mode="before")(
        _coerce_enum(DocStatus, "active"))


# ── 블록 5. 출처 (혼합) ──────────────────────────────────────────────────────
class ProvenanceBlock(BaseModel):
    source_system: Optional[str] = None
    author: Optional[str] = None
    auto_filled: list[AutoFilledField] = Field(default_factory=list)


class DocumentMetadata(BaseModel):
    """문서 단위 전체 메타데이터."""

    identification: IdentificationBlock
    classification: ClassificationBlock = Field(default_factory=ClassificationBlock)
    governance: GovernanceBlock = Field(default_factory=GovernanceBlock)
    lifecycle: LifecycleBlock = Field(default_factory=LifecycleBlock)
    provenance: ProvenanceBlock = Field(default_factory=ProvenanceBlock)


# ── 청크 레벨 ────────────────────────────────────────────────────────────────
class ChunkMetadata(BaseModel):
    chunk_id: str
    parent_doc_id: str
    chunk_type: ChunkType = ChunkType.TEXT
    section_title: Optional[str] = None
    page_no: Optional[int] = None

    def to_qdrant_payload(self, doc: DocumentMetadata) -> dict:
        """청크 payload = 청크 고유 필드 + 부모 문서에서 상속한 doc-level 필드."""
        return {
            "chunk_id": self.chunk_id,
            "parent_doc_id": self.parent_doc_id,
            "chunk_type": self.chunk_type.value,
            "section_title": self.section_title,
            "page_no": self.page_no,
            **doc_level_payload(doc),
        }


def doc_level_payload(doc: DocumentMetadata) -> dict:
    """문서 단위 payload 필드(접근통제·생애주기·인용용). 모든 청크가 공유하며,
    문서 관리에서 메타데이터/상태가 바뀌면 이 필드만 Qdrant에 set_payload 로 갱신한다."""
    gov = doc.governance
    life = doc.lifecycle
    cls = doc.classification
    return {
        # 접근통제 (하드 필터) — 조직도 열람 토큰. 빈 값이면 팀 전체("*").
        "access_groups": list(gov.access_tokens) or ["*"],
        # 생애주기 (기본 검색 제외 조건)
        "status": life.status.value,
        "expiry_date": life.expiry_date.isoformat() if life.expiry_date else None,
        "superseded_by": life.superseded_by,
        # 폴더(조직노드) 스코프 검색용
        "author_node_id": gov.author_node_id,
        # 인용·평가용
        "doc_type": doc.classification.doc_type.value,
        "title": cls.title_normalized,
        "source_filename": doc.identification.source_filename,
        # 검색 보조(키워드) — 임베딩 텍스트 합류는 pipeline.index 에서 처리
        "keywords": list(cls.keywords),
    }
