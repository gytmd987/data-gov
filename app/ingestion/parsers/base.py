"""파서 공통 인터페이스.

모든 포맷 파서는 원본 파일을 읽어 **구조를 보존한 ParsedElement 리스트**로 변환한다.
후속 청킹 단계가 이 요소들을 chunk_type/섹션/페이지 정보와 함께 청크로 묶는다.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from pydantic import BaseModel

from app.schemas.enums import ChunkType, FileFormat


class ParsedElement(BaseModel):
    """파싱된 문서 조각 1개(문단/표/리스트/이미지 캡션 등)."""

    text: str
    element_type: ChunkType = ChunkType.TEXT
    section_title: Optional[str] = None
    page_no: Optional[int] = None


class ParseResult(BaseModel):
    elements: list[ParsedElement]
    page_count: Optional[int] = None


@runtime_checkable
class Parser(Protocol):
    """포맷별 파서 프로토콜."""

    file_format: FileFormat

    def parse(self, path: str) -> ParseResult: ...
