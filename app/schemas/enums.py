"""문서 메타데이터용 vocabulary(enum).

편집 가능한 어휘(doc_type / sensitivity_level / pii_type)는 **config/system.yaml에서
동적으로 생성**된다 → YAML만 편집하면 값이 추가/삭제되고 UI·검증에 자동 반영.
구조적 enum(file_format / language / doc_status / chunk_type)은 코드에 고정.

설계 원칙: LLM 자동 채움은 이 vocabulary 안에서만 값을 채운다(자유 텍스트 금지).
"""

from __future__ import annotations

import re
from enum import Enum

from app import system_config


def _str_enum(name: str, values: list[str]) -> type[Enum]:
    """문자열 값 리스트로 str-enum을 생성. 멤버명은 값의 대문자(비식별자 문자는 _)."""
    members: dict[str, str] = {}
    for v in values:
        key = re.sub(r"\W", "_", str(v)).upper()
        members.setdefault(key, v)
    return Enum(name, members, type=str)


# ── 설정 기반(편집 가능) ─────────────────────────────────────────────────────
DocType = _str_enum("DocType", system_config.doc_types())
SensitivityLevel = _str_enum("SensitivityLevel", system_config.sensitivity_levels())
PiiType = _str_enum("PiiType", system_config.pii_types())


# ── 구조적(코드 고정) ────────────────────────────────────────────────────────
class FileFormat(str, Enum):
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    PDF = "pdf"
    JPG = "jpg"
    PNG = "png"
    TXT = "txt"


class Language(str, Enum):
    KO = "ko"
    EN = "en"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class DocStatus(str, Enum):
    """문서 생애주기 상태. 기본 검색은 active 만 노출."""

    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"     # 다른 문서로 대체됨
    EXPIRED = "expired"
    ARCHIVED = "archived"


class ChunkType(str, Enum):
    TEXT = "text"
    TABLE = "table"
    LIST = "list"
    IMAGE_CAPTION = "image_caption"
