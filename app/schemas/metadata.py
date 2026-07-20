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

from pydantic import BaseModel, Field

from .enums import (
    ChunkType,
    DocStatus,
    DocType,
    FileFormat,
    Language,
    PiiType,
    SensitivityLevel,
)


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
    ingested_at: datetime
    ingested_by: str
    page_count: Optional[int] = None


# ── 블록 2. 내용 분류 (LLM 추론 → 사람 확인) ────────────────────────────────
class ClassificationBlock(BaseModel):
    doc_type: DocType = DocType.UNKNOWN
    title_normalized: Optional[str] = None
    summary: Optional[str] = None
    department: Optional[str] = None
    team: Optional[str] = None
    topics: list[str] = Field(default_factory=list)   # controlled tag (사전 검증은 validator에서)
    language: Language = Language.UNKNOWN


# ── 블록 3. 거버넌스·접근통제 (사람 필수 확인) ──────────────────────────────
class GovernanceBlock(BaseModel):
    sensitivity_level: Optional[SensitivityLevel] = None   # 필수 (미확정 시 적재 차단)
    contains_pii: Optional[bool] = None                    # 필수
    pii_types: list[PiiType] = Field(default_factory=list)
    access_groups: list[str] = Field(default_factory=list) # 필수 (비어 있으면 차단)
    owner: Optional[str] = None                            # 필수


# ── 블록 4. 생애주기 (LLM 추론 + 사람 확인) ─────────────────────────────────
class LifecycleBlock(BaseModel):
    effective_date: Optional[date] = None
    review_date: Optional[date] = None
    expiry_date: Optional[date] = None
    version: Optional[str] = None
    status: DocStatus = DocStatus.DRAFT
    supersedes: Optional[str] = None       # 이 문서가 대체하는 doc_id
    superseded_by: Optional[str] = None     # 이 문서를 대체한 doc_id


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
        """청크 payload에 부모 문서의 접근통제·생애주기 필드를 상속시켜,
        Qdrant 검색 시 하드 필터링(권한/민감도/만료/대체)에 바로 사용한다."""
        gov = doc.governance
        life = doc.lifecycle
        return {
            "chunk_id": self.chunk_id,
            "parent_doc_id": self.parent_doc_id,
            "chunk_type": self.chunk_type.value,
            "section_title": self.section_title,
            "page_no": self.page_no,
            # 접근통제 (하드 필터)
            "access_groups": gov.access_groups,
            "sensitivity_rank": gov.sensitivity_level.rank if gov.sensitivity_level else None,
            # 생애주기 (기본 검색 제외 조건)
            "status": life.status.value,
            "expiry_date": life.expiry_date.isoformat() if life.expiry_date else None,
            "superseded_by": life.superseded_by,
            # 인용·평가용
            "doc_type": doc.classification.doc_type.value,
            "title": doc.classification.title_normalized,
            "source_filename": doc.identification.source_filename,
        }
