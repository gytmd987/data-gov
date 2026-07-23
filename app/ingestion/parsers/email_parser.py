"""이메일(.eml) 파서 — 표준 라이브러리 email 로 제목·본문(text/plain) 추출.

첨부는 무시하고 본문 텍스트만 뽑는다. 본문이 비면 요소 없음 → 상위에서 읽기 실패로 처리.
"""

from __future__ import annotations

from email import message_from_binary_file
from email.policy import default as default_policy
from pathlib import Path

from app.schemas.enums import ChunkType, FileFormat

from .base import ParsedElement, ParseResult


class EmailParser:
    file_format = FileFormat.EMAIL

    def parse(self, path: str) -> ParseResult:
        with open(path, "rb") as f:
            msg = message_from_binary_file(f, policy=default_policy)

        elements: list[ParsedElement] = []
        header = " · ".join(
            f"{k}: {msg[k]}" for k in ("From", "To", "Date", "Subject") if msg[k])
        subject = str(msg["Subject"] or "").strip()
        if header:
            elements.append(ParsedElement(
                text=header, element_type=ChunkType.TEXT, section_title=subject or None))

        def _decode(part) -> str:
            payload = part.get_payload(decode=True)
            if payload is None:
                return ""
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except LookupError:
                return payload.decode("utf-8", errors="replace")

        body = ""
        if msg.is_multipart():
            plain = [p for p in msg.walk() if p.get_content_type() == "text/plain"]
            html = [p for p in msg.walk() if p.get_content_type() == "text/html"]
            body = _decode((plain or html or [msg])[0])
        else:
            body = _decode(msg)
        body = (body or "").strip()
        if body:
            elements.append(ParsedElement(
                text=body, element_type=ChunkType.TEXT, section_title=subject or None))

        return ParseResult(elements=elements, page_count=None)
