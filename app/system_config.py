"""중앙 설정 로더 (config/system.yaml).

메타데이터 어휘·거버넌스 필수필드·권한(직책×직무) 규칙의 단일 진실 공급원.
enums, validator, 검토 UI, 권한 부여가 모두 이 파일을 참조하므로 YAML만 편집하면
코드 수정 없이 항목/값/권한이 바뀐다.

경로: 환경변수 SYSTEM_CONFIG_PATH > 리포지토리 루트의 config/system.yaml
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "system.yaml"


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    path = Path(os.environ.get("SYSTEM_CONFIG_PATH", _DEFAULT_PATH))
    if not path.exists():
        raise FileNotFoundError(
            f"시스템 설정 파일을 찾을 수 없습니다: {path} "
            f"(SYSTEM_CONFIG_PATH 로 경로 지정 가능)")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def reload() -> None:
    """설정 파일을 다시 읽는다(편집 후 캐시 무효화)."""
    _load.cache_clear()


# ── 메타데이터 어휘 ──────────────────────────────────────────────────────────
def doc_types() -> list[str]:
    return list(_load()["metadata"]["doc_types"])


def title_cleanup() -> dict[str, Any]:
    """제목 정리 규칙(꼬리표·복사흔적·장식문자·무의미 파일명·날짜 오탐 방지)."""
    return dict(_load()["metadata"].get("title_cleanup") or {})


def doc_type_hints() -> dict[str, str]:
    """문서 종류별 판별 기준(AI 분류 프롬프트에 주입). 없으면 빈 dict."""
    return dict(_load()["metadata"].get("doc_type_hints") or {})


def departments() -> list[str]:
    return list(_load()["metadata"].get("departments") or [])


# ── 화면 표시용 한글 라벨 ────────────────────────────────────────────────────
def labels() -> dict[str, str]:
    return dict(_load().get("labels") or {})


def label(value: Any) -> str:
    """값의 한글 표시 라벨. 매핑에 없으면 원값 그대로."""
    key = getattr(value, "value", value)
    return labels().get(key, key)


# ── 관리자 · 조직 역할 ───────────────────────────────────────────────────────
def admin_emails() -> list[str]:
    """관리자(관리 콘솔 접근 허용) 이메일 목록."""
    return list(_load()["permissions"].get("admins") or [])


def org_roles() -> list[str]:
    """조직 역할 목록(팀장/그룹장/파트장/파트원). 없으면 코드 기본값."""
    from app.org.tree import ROLES
    return list(_load()["permissions"].get("org_roles") or ROLES)
