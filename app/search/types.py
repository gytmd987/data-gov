"""검색 파이프라인 공통 타입."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class RetrievedChunk(BaseModel):
    chunk_id: str
    text: str
    score: float
    payload: dict[str, Any] = Field(default_factory=dict)

    # 인용 편의 접근자
    @property
    def doc_id(self) -> Optional[str]:
        return self.payload.get("parent_doc_id")

    @property
    def title(self) -> Optional[str]:
        return self.payload.get("title")

    @property
    def source_filename(self) -> Optional[str]:
        return self.payload.get("source_filename")

    @property
    def page_no(self) -> Optional[int]:
        return self.payload.get("page_no")


class Citation(BaseModel):
    marker: int                 # 본문 각주 번호 [1], [2] ...
    doc_id: Optional[str] = None
    title: Optional[str] = None
    page_no: Optional[int] = None
    chunk_id: str


class Answer(BaseModel):
    text: str
    citations: list[Citation] = Field(default_factory=list)
    used_chunks: list[RetrievedChunk] = Field(default_factory=list)
