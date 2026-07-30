"""docx 파서: 헤딩 스타일로 섹션을 추적하고, 표는 별도 table 요소로 추출.

본문에 삽입된 이미지(캡처·도표·스캔본)는 image_ocr 콜백이 있으면 OCR해서
IMAGE_CAPTION 요소로 넣는다. 콜백이 없으면 이미지는 건너뛴다(텍스트 경로만).
"""

from __future__ import annotations

from typing import Callable, Optional

import docx

from app.schemas.enums import ChunkType, FileFormat

from .base import ParsedElement, ParseResult

ImageOCR = Callable[[bytes], str]

# 로고·아이콘 같은 장식 이미지는 OCR 비용만 쓰고 얻는 게 없다 → 이보다 작으면 건너뜀
_MIN_IMAGE_BYTES = 8 * 1024


def _table_to_markdown(table) -> str:
    rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    header, *body = rows
    lines = ["| " + " | ".join(header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)


def _image_blobs(document) -> list[bytes]:
    """문서에 포함된 이미지 원본 바이트 목록(중복 제거, 등장 순서 유지)."""
    blobs: list[bytes] = []
    seen: set[str] = set()
    for rel in document.part.rels.values():
        if "image" not in rel.reltype:
            continue
        try:
            blob = rel.target_part.blob
        except Exception:      # 깨진 관계·외부 링크 이미지 등은 조용히 건너뜀
            continue
        key = rel.target_part.partname
        if str(key) in seen:
            continue
        seen.add(str(key))
        blobs.append(blob)
    return blobs


class DocxParser:
    file_format = FileFormat.DOCX

    def __init__(self, image_ocr: Optional[ImageOCR] = None) -> None:
        self._image_ocr = image_ocr

    def parse(self, path: str) -> ParseResult:
        doc = docx.Document(path)
        elements: list[ParsedElement] = []
        current_section: str | None = None

        for para in doc.paragraphs:
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name or "").lower() if para.style else ""
            if style.startswith("heading") or style.startswith("title"):
                current_section = text
                elements.append(
                    ParsedElement(text=text, element_type=ChunkType.TEXT,
                                  section_title=current_section)
                )
            else:
                etype = ChunkType.LIST if style.startswith("list") else ChunkType.TEXT
                elements.append(
                    ParsedElement(text=text, element_type=etype,
                                  section_title=current_section)
                )

        for table in doc.tables:
            md = _table_to_markdown(table)
            if md:
                elements.append(
                    ParsedElement(text=md, element_type=ChunkType.TABLE,
                                  section_title=current_section)
                )

        # 본문 이미지 OCR — 워드에 캡처·스캔본을 붙여 넣은 문서가 흔하다.
        # 이걸 빼면 그 내용은 검색에 전혀 안 잡힌다(같은 내용을 PDF로 올리면 잡히는데도).
        if self._image_ocr is not None:
            for i, blob in enumerate(_image_blobs(doc), start=1):
                if len(blob) < _MIN_IMAGE_BYTES:
                    continue
                try:
                    caption = self._image_ocr(blob).strip()
                except Exception:
                    caption = ""
                if caption:
                    elements.append(ParsedElement(
                        text=caption, element_type=ChunkType.IMAGE_CAPTION,
                        section_title=f"이미지 {i}"))

        return ParseResult(elements=elements, page_count=None)
