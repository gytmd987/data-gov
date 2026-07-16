"""이미지(jpg/png) 파서: 문서 파서(PaddleOCR-VL 등) OCR 콜백으로 텍스트/표를 추출."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from app.schemas.enums import ChunkType, FileFormat

from .base import ParsedElement, ParseResult

ImageOCR = Callable[[bytes], str]


class ImageParser:
    """jpg/png 공용. file_format 은 생성 시 지정."""

    def __init__(self, image_ocr: ImageOCR, file_format: FileFormat = FileFormat.JPG) -> None:
        self._image_ocr = image_ocr
        self.file_format = file_format

    def parse(self, path: str) -> ParseResult:
        blob = Path(path).read_bytes()
        text = self._image_ocr(blob).strip()
        elements = []
        if text:
            elements.append(ParsedElement(
                text=text, element_type=ChunkType.IMAGE_CAPTION, page_no=1))
        return ParseResult(elements=elements, page_count=1)
