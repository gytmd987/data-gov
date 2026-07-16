"""pdf 파서.

- 텍스트 레이어가 있는 PDF: PyMuPDF로 페이지별 텍스트 추출.
- 스캔(텍스트 레이어 없음) 페이지: page_ocr 콜백(PaddleOCR-VL 등)이 주어지면 해당 페이지
  이미지를 OCR한다. 콜백이 없으면 빈 페이지로 남겨두고 후속 단계에서 경고한다.

표 구조 인식이 중요한 경우 Docling 파서로 교체할 수 있도록 인터페이스를 동일하게 유지한다.
"""

from __future__ import annotations

from typing import Callable, Optional

import fitz  # PyMuPDF

from app.schemas.enums import ChunkType, FileFormat

from .base import ParsedElement, ParseResult

# (page_image_png_bytes) -> markdown/text
PageOCR = Callable[[bytes], str]

# 이 임계치보다 텍스트가 적으면 스캔 페이지로 간주하고 OCR 시도
_MIN_TEXT_LEN = 20


class PdfParser:
    file_format = FileFormat.PDF

    def __init__(self, page_ocr: Optional[PageOCR] = None) -> None:
        self._page_ocr = page_ocr

    def parse(self, path: str) -> ParseResult:
        elements: list[ParsedElement] = []
        with fitz.open(path) as doc:
            page_count = doc.page_count
            for i, page in enumerate(doc, start=1):
                text = page.get_text("text").strip()
                if len(text) >= _MIN_TEXT_LEN:
                    elements.append(ParsedElement(
                        text=text, element_type=ChunkType.TEXT, page_no=i))
                elif self._page_ocr is not None:
                    png = page.get_pixmap(dpi=200).tobytes("png")
                    ocr_text = self._page_ocr(png).strip()
                    if ocr_text:
                        elements.append(ParsedElement(
                            text=ocr_text, element_type=ChunkType.TEXT, page_no=i))
                # else: 스캔 페이지 + OCR 미주입 → 빈 페이지(파이프라인이 경고)
        return ParseResult(elements=elements, page_count=page_count)
