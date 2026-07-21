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


def sensitivity_levels() -> list[str]:
    """낮음→높음 순서."""
    return list(_load()["metadata"]["sensitivity_levels"])


def pii_types() -> list[str]:
    return list(_load()["metadata"]["pii_types"])


def topics() -> list[str]:
    return list(_load()["metadata"].get("topics") or [])


def departments() -> list[str]:
    return list(_load()["metadata"].get("departments") or [])


# ── 화면 표시용 한글 라벨 ────────────────────────────────────────────────────
def labels() -> dict[str, str]:
    return dict(_load().get("labels") or {})


def label(value: Any) -> str:
    """값의 한글 표시 라벨. 매핑에 없으면 원값 그대로."""
    key = getattr(value, "value", value)
    return labels().get(key, key)


def sensitivity_rank(level: str | None) -> int:
    """민감도 등급의 순위(0=가장 낮음). 없는 값은 -1."""
    if level is None:
        return -1
    levels = sensitivity_levels()
    key = getattr(level, "value", level)
    return levels.index(key) if key in levels else -1


# ── 거버넌스 ─────────────────────────────────────────────────────────────────
def required_governance_fields() -> list[str]:
    return list(_load()["governance"]["required_fields"])


# ── 권한 (직책 × 직무) ───────────────────────────────────────────────────────
def access_groups() -> list[str]:
    return list(_load()["permissions"]["access_groups"])


def admin_emails() -> list[str]:
    """관리자(문서관리·사용자관리 접근 허용) 이메일 목록."""
    return list(_load()["permissions"].get("admins") or [])


def positions() -> list[str]:
    return list(_load()["permissions"]["positions"])


def jobs() -> list[str]:
    return list(_load()["permissions"]["jobs"])


def _matches(value: str, patterns: list[str]) -> bool:
    return "*" in patterns or value in patterns


def resolve_access(position: str, job: str) -> tuple[set[str], str]:
    """(직책, 직무) → (접근그룹 집합, 최대 열람 민감도 clearance).

    매칭되는 모든 규칙의 groups를 합치고 clearance는 가장 높은 등급을 취한다.
    매칭 규칙이 없으면 접근그룹 없음 + 가장 낮은 등급.
    """
    perms = _load()["permissions"]
    groups: set[str] = set()
    best_rank = -1
    clearance = sensitivity_levels()[0]

    for rule in perms.get("rules", []):
        if _matches(position, rule.get("positions", [])) and \
           _matches(job, rule.get("jobs", [])):
            groups.update(rule.get("groups", []))
            c = rule.get("clearance")
            if c is not None and sensitivity_rank(c) > best_rank:
                best_rank = sensitivity_rank(c)
                clearance = c
    return groups, clearance
