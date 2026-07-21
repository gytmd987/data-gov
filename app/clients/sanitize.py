"""외부 서비스 전송 전 텍스트 정리.

문서에서 추출한 텍스트에 surrogate 문자(U+D800~U+DFFF)가 섞이면 JSON 직렬화 시
UnicodeEncodeError가 난다. TEI/vLLM에 보내기 전에 제거한다.
"""

from __future__ import annotations

import re

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def strip_surrogates(text: str) -> str:
    return _SURROGATE_RE.sub("", text)


def clean_texts(texts: list[str]) -> list[str]:
    return [strip_surrogates(t) for t in texts]
