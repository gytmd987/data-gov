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
# 닫는 태그가 없는 요소(void). 열림/닫힘을 세면 안 된다 — 세면 짝이 안 맞는다.
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}
# 줄바꿈을 만드는 블록 요소
_BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
          "table", "section", "article", "header", "footer", "blockquote", "pre"}

# 메일에서 '인용된 이전 메일'을 감싸는 요소. 메일 클라이언트마다 이름이 다르다.
_QUOTE_TAGS = {"blockquote"}
_QUOTE_MARKERS = ("gmail_quote", "moz-cite-prefix", "yahoo_quoted",
                  "divrplyfwdmsg", "olk_src_body_section", "appendonsend",
                  "gmail_attr")


def _is_quote_container(tag: str, attrs) -> bool:
    if tag in _QUOTE_TAGS:
        return True
    joined = " ".join(v for k, v in attrs if k in ("class", "id") and v).lower()
    return any(m in joined for m in _QUOTE_MARKERS)


class _Extractor(HTMLParser):
    """열린 태그를 스택으로 추적한다.

    깊이만 세면 메일 HTML 처럼 닫는 태그가 빠진 마크업에서 짝이 어긋나, 한 번
    버리기 시작하면 그 뒤 본문이 통째로 사라진다. 스택을 쓰면 바깥 태그가 닫힐 때
    안쪽도 함께 닫힌 것으로 처리돼 그런 일이 없다.
    """

    def __init__(self, drop_quotes: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._open: list[str] = []       # 열려 있는 태그 이름
        self._skip_at: int | None = None  # 이 위치에서 버리기 시작했다
        self._drop_quotes = drop_quotes

    @property
    def _skipping(self) -> bool:
        return self._skip_at is not None

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            if not self._skipping and tag == "br":
                self._parts.append("\n")
            return
        self._open.append(tag)
        if self._skipping:
            return
        if tag in _DROP or (self._drop_quotes and _is_quote_container(tag, attrs)):
            self._skip_at = len(self._open) - 1
            return
        if tag == "li":
            self._parts.append("\n- ")
        elif tag in ("td", "th"):
            self._parts.append("\t")
        elif tag in _BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _VOID:
            return
        if tag in self._open:
            # 짝이 안 맞아도 안쪽 태그들은 함께 닫힌 것으로 본다
            idx = len(self._open) - 1 - self._open[::-1].index(tag)
            del self._open[idx:]
            if self._skip_at is not None and self._skip_at >= len(self._open):
                self._skip_at = None
                return
        if self._skipping:
            return
        if tag in _BLOCK:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._skipping and data:
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


def html_to_text(html: str, drop_quotes: bool = False) -> str:
    """HTML 본문 → 사람이 보는 글자만. 실패해도 예외를 던지지 않는다.

    drop_quotes=True 면 인용된 이전 메일(blockquote 등)을 통째로 버린다.
    """
    if not html:
        return ""
    # Outlook 조건부 주석 등 주석은 통째로 제거(안에 마크업이 들어 있다)
    cleaned = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    parser = _Extractor(drop_quotes=drop_quotes)
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
