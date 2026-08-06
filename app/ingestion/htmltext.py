"""HTML → 읽을 수 있는 텍스트.

회사 메일은 대부분 HTML 로 발송돼서, 본문을 그대로 뽑으면 태그·스타일·조건부 주석이
섞인 마크업이 나온다. 그 상태로 AI 에 넘기면 "본문은 이메일 클라이언트 렌더링 코드로
채워져 있으나…" 같은 엉뚱한 요약이 나온다. 사람이 화면에서 보는 글자만 남긴다.

표준 라이브러리(html.parser)만 쓴다 — 폐쇄망에 새 의존성을 들이지 않기 위함.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

# 내용이 아니라 코드/장식인 요소 — 여는~닫는 태그 사이를 통째로 버린다
_DROP = {"script", "style", "head", "title", "noscript", "svg"}
# 닫는 태그가 없는 요소(void). 깊이를 세면 안 된다 — 세면 그 뒤 문서 전체가 사라진다.
_VOID_DROP = {"meta", "link", "base", "col", "source", "track", "wbr"}
# 줄바꿈을 만드는 블록 요소
_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
          "table", "section", "article", "header", "footer", "blockquote", "pre"}


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _VOID_DROP:
            return
        if tag in _DROP:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "li":
            self._parts.append("\n- ")
        elif tag in ("td", "th"):
            self._parts.append("\t")
        elif tag in _BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag in _BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and data:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def looks_like_html(text: str) -> bool:
    """태그가 실제로 쓰인 문서인가(꺾쇠 몇 개 있다고 HTML 로 보지 않는다)."""
    if not text:
        return False
    sample = text[:4000].lower()
    if "<html" in sample or "<!doctype html" in sample or "<body" in sample:
        return True
    return len(re.findall(r"</?(p|div|br|table|tr|td|span|a|img|h[1-6])\b", sample)) >= 3


def html_to_text(html: str) -> str:
    """HTML 본문 → 사람이 보는 글자만. 실패해도 예외를 던지지 않는다."""
    if not html:
        return ""
    # Outlook 조건부 주석 등 주석은 통째로 제거(안에 마크업이 들어 있다)
    cleaned = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    parser = _Extractor()
    try:
        parser.feed(cleaned)
        parser.close()
        text = parser.text()
    except Exception:      # noqa: BLE001 — 깨진 HTML 이어도 태그만 걷어내 진행
        text = re.sub(r"<[^>]+>", " ", cleaned)
    return normalize(unescape(text))


def normalize(text: str) -> str:
    """공백·빈 줄 정리. 표 셀 구분(탭)은 유지한다."""
    text = text.replace(" ", " ").replace("\r", "")
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    lines = [ln.rstrip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln.strip()).strip()


def to_text(content: str) -> str:
    """HTML 이면 텍스트로 바꾸고, 아니면 공백만 정리해 돌려준다."""
    return html_to_text(content) if looks_like_html(content) else normalize(content)
