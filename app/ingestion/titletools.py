"""문서 제목 구성 — 파일명 기반 + 날짜 정규화.

규칙(사용자 확정):
- 제목은 원본 파일명(확장자 제외)을 최대한 그대로 쓴다(내용을 크게 바꾸지 않음).
- 파일명에 없는 중요한 정보만 추가한다. 핵심은 '날짜'.
- 날짜는 무조건 제일 앞에 `(YY-MMDD)` 형식으로 통일한다.
  파일명에 이미 날짜가 있어도 이 형식으로 바꿔 앞으로 옮긴다.
- 파일명에 날짜가 없으면 AI가 추출한 문서 날짜를 쓴다(그것도 없으면 날짜 없이).
- 제목이 너무 길어지지 않게 길이를 제한한다.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

_MAX_TITLE_LEN = 60

# 파일명이 내용을 알 수 없는 값 → 이 경우엔 AI 제안 제목을 base 로 쓴다.
_UNINFORMATIVE = {
    "새문서", "새 문서", "무제", "제목없음", "제목 없음", "문서", "document",
    "document1", "새파일", "새 파일", "untitled", "noname", "scan", "이미지",
}

# 날짜 후보 패턴(먼저 매칭되는 것 우선). 모두 (year, month, day) 그룹을 준다.
_DATE_PATTERNS = [
    # 이미 정규화된 (YY-MMDD)
    re.compile(r"\((?P<y>\d{2})-(?P<m>\d{2})(?P<d>\d{2})\)"),
    # 2025년 7월 28일
    re.compile(r"(?P<y>\d{4})\s*년\s*(?P<m>\d{1,2})\s*월\s*(?P<d>\d{1,2})\s*일?"),
    # 2025-07-28 / 2025.07.28 / 2025/07/28 / 2025 07 28
    re.compile(r"(?<!\d)(?P<y>\d{4})[.\-/ ](?P<m>\d{1,2})[.\-/ ](?P<d>\d{1,2})(?!\d)"),
    # 20250728
    re.compile(r"(?<!\d)(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})(?!\d)"),
    # 25-07-28 / 25.7.28
    re.compile(r"(?<!\d)(?P<y>\d{2})[.\-/](?P<m>\d{1,2})[.\-/](?P<d>\d{1,2})(?!\d)"),
    # 250728
    re.compile(r"(?<!\d)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?!\d)"),
]


def _mk_date(y: int, m: int, d: int) -> Optional[date]:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def extract_date(text: str) -> tuple[Optional[date], str]:
    """텍스트에서 첫 날짜를 찾아 (date, 날짜를 제거한 텍스트) 반환. 없으면 (None, text)."""
    for pat in _DATE_PATTERNS:
        for mo in pat.finditer(text):
            d = _mk_date(int(mo.group("y")), int(mo.group("m")), int(mo.group("d")))
            if d is not None:
                cleaned = (text[:mo.start()] + " " + text[mo.end():])
                return d, cleaned
    return None, text


def _tidy(text: str) -> str:
    """날짜 제거 후 남은 구분자/공백 정리."""
    text = re.sub(r"[\s_\-.]*[\[\](){}][\s_\-.]*", " ", text)  # 빈 괄호류 정리
    text = re.sub(r"[\s_]+", " ", text)                        # 공백/언더스코어 정규화
    return text.strip(" -_.\t")


def strip_date_prefix(title: str) -> str:
    """제목 앞의 `(YY-MMDD) ` 접두를 제거한 본문(같은 문서의 다른 버전 판별용)."""
    return re.sub(r"^\(\d{2}-\d{4}\)\s*", "", (title or "").strip()).strip()


def safe_filename(name: str, max_len: int = 80) -> str:
    """파일 시스템/다운로드용으로 안전한 파일명(제목 기반). 확장자는 호출측에서 붙인다."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:max_len].strip() or "document"


def parse_iso(value) -> Optional[date]:
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_date_loose(value) -> Optional[date]:
    """사람이 입력/화면에서 복사한 어떤 날짜 표기든 date 로 해석한다.

    'YYYY-MM-DD' 뿐 아니라 '2026년 6월 30일', '2026.06.30', '20260630' 등도 허용한다.
    (화면이 한국어 로케일로 날짜를 렌더링해도 저장이 깨지지 않도록.)
    해석 불가면 None.
    """
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    return parse_iso(text) or extract_date(text)[0]


def to_iso(value) -> Optional[str]:
    """느슨한 날짜 입력 → 'YYYY-MM-DD' 문자열(해석 실패 시 None)."""
    d = parse_date_loose(value)
    return d.isoformat() if d else None


def compose_title(filename: str, ai_title: Optional[str] = None,
                  ai_date=None, max_len: int = _MAX_TITLE_LEN) -> str:
    """파일명 + (파일명/AI) 날짜 → `(YY-MMDD) 제목` 형태의 통일된 제목.

    - 파일명 어간을 base 로 쓰되, 파일명이 의미 없으면 AI 제안 제목을 base 로.
    - 날짜는 파일명에 있으면 그걸 정규화, 없으면 AI 추출 날짜를 사용해 맨 앞에 붙인다.
    """
    stem = Path(filename).stem.strip()
    d_in_name, cleaned = extract_date(stem)
    base = _tidy(cleaned)

    # 파일명이 비었거나 의미 없으면 AI 제안 제목으로 대체
    if (not base or base.lower() in _UNINFORMATIVE) and ai_title:
        base = ai_title.strip()

    doc_date = d_in_name or parse_iso(ai_date)
    prefix = f"({doc_date:%y-%m%d}) " if doc_date else ""
    title = (prefix + base).strip()
    if len(title) > max_len:
        title = title[:max_len].rstrip(" -_.")
    return title or stem or (ai_title or "").strip()
