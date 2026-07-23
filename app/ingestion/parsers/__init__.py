"""포맷별 파서 레지스트리.

OCR/VLM이 필요한 파서(pdf 스캔, pptx 이미지, jpg/png)는 image_ocr/page_ocr 콜백을 주입한다.
콜백은 PaddleOCR-VL 서비스 호출을 감싼 함수다(app.ingestion.ocr 참고). 미주입 시 텍스트 경로만 동작.
"""

from __future__ import annotations

from typing import Callable, Optional

from app.schemas.enums import FileFormat

from .base import ParsedElement, ParseResult, Parser
from .docx_parser import DocxParser
from .email_parser import EmailParser
from .image_parser import ImageParser
from .pdf_parser import PdfParser
from .pptx_parser import PptxParser
from .text_parser import TextParser
from .xlsx_parser import XlsxParser

OCRFn = Callable[[bytes], str]


class UnsupportedFormatError(ValueError):
    pass


def build_registry(ocr: Optional[OCRFn] = None) -> dict[FileFormat, Parser]:
    """포맷 → 파서 인스턴스 매핑을 만든다. ocr 콜백이 있으면 스캔/이미지 경로도 활성화."""
    registry: dict[FileFormat, Parser] = {
        FileFormat.TXT: TextParser(),
        FileFormat.DOCX: DocxParser(),
        FileFormat.XLSX: XlsxParser(),
        FileFormat.PPTX: PptxParser(image_ocr=ocr),
        FileFormat.PDF: PdfParser(page_ocr=ocr),
        FileFormat.EMAIL: EmailParser(),
    }
    if ocr is not None:
        registry[FileFormat.JPG] = ImageParser(ocr, FileFormat.JPG)
        registry[FileFormat.PNG] = ImageParser(ocr, FileFormat.PNG)
    return registry


def get_parser(file_format: FileFormat, ocr: Optional[OCRFn] = None) -> Parser:
    """포맷에 맞는 파서 인스턴스를 반환한다. OCR 필요한 포맷은 ocr 콜백 주입 필요."""
    parser = build_registry(ocr=ocr).get(file_format)
    if parser is None:
        raise UnsupportedFormatError(
            f"지원하지 않거나 OCR 미설정 포맷: {file_format.value}"
        )
    return parser


def build_parser_content(result: ParseResult) -> str:
    """LLM 자동 채움 입력용으로 파싱 요소 텍스트를 이어 붙인다(섹션 제목 포함)."""
    lines: list[str] = []
    for el in result.elements:
        if el.section_title:
            lines.append(f"# {el.section_title}")
        lines.append(el.text)
    return "\n\n".join(lines)


__all__ = [
    "ParsedElement", "ParseResult", "Parser",
    "build_registry", "get_parser", "build_parser_content",
    "UnsupportedFormatError",
]
