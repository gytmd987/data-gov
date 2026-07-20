"""골드셋 스키마·로더.

문서는 source_filename(안정 식별자)으로 참조한다. doc_id는 적재마다 바뀌는 UUID라 부적합.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


class GoldItem(BaseModel):
    id: str
    query: str
    user_id: str                                   # 질의 주체(접근통제 반영)
    relevant_filenames: list[str] = Field(default_factory=list)   # 정답 근거 문서
    forbidden_filenames: list[str] = Field(default_factory=list)  # 절대 노출되면 안 되는 문서
    expected_answer_contains: list[str] = Field(default_factory=list)  # 답변 포함 기대 문자열


class GoldSet(BaseModel):
    items: list[GoldItem] = Field(default_factory=list)


def load_goldset(path: str | Path) -> GoldSet:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return GoldSet.model_validate(data)
