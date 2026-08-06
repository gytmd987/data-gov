"""이메일(.eml) 파서 — 제목·헤더 + 본문 텍스트 추출.

회사 메일은 대부분 HTML 로 온다. 마크업을 그대로 넘기면 AI 가 "본문이 렌더링 코드로
채워져 있다"는 식으로 요약하므로, **HTML 은 사람이 보는 글자로 변환**해서 넘긴다.
text/plain 이 있어도 껍데기(한 줄짜리 안내)뿐인 메일이 많아, 두 본문을 모두 뽑아
**내용이 더 실한 쪽**을 쓴다. 첨부는 무시한다.
"""

from __future__ import annotations

from email import message_from_binary_file
from email.policy import default as default_policy

from app.ingestion.htmltext import html_to_text, normalize
from app.schemas.enums import ChunkType, FileFormat

from .base import ParsedElement, ParseResult

# text/plain 이 이보다 짧으면 껍데기("본 메일은 HTML 형식입니다")로 본다
_STUB_LEN = 80
# plain 이 짧지 않아도 HTML 본문이 이 배수 이상 길면 HTML 쪽에 실제 내용이 있다고 본다
_RICHER_RATIO = 3


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

        def _first(content_type: str) -> str:
            parts = [p for p in msg.walk() if p.get_content_type() == content_type
                     and "attachment" not in str(p.get("Content-Disposition", ""))]
            return _decode(parts[0]) if parts else ""

        if msg.is_multipart():
            plain = normalize(_first("text/plain"))
            rich = html_to_text(_first("text/html"))
        else:
            raw = _decode(msg)
            if msg.get_content_type() == "text/html":
                plain, rich = "", html_to_text(raw)
            else:
                plain, rich = normalize(raw), ""

        # 원칙은 text/plain(발신자가 의도한 본문). 다만 그게 껍데기뿐이거나 HTML 쪽에만
        # 실제 내용이 있는 회사 메일이 흔해서, 그럴 때만 HTML 본문으로 바꾼다.
        body = plain
        if rich and (len(plain) < _STUB_LEN or len(rich) >= len(plain) * _RICHER_RATIO):
            if len(rich) > len(plain):
                body = rich
        body = (body or "").strip()
        if body:
            elements.append(ParsedElement(
                text=body, element_type=ChunkType.TEXT, section_title=subject or None))

        return ParseResult(elements=elements, page_count=None)
