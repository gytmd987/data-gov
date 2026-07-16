"""LLM 자동 메타데이터 채움 (controlled schema 안에서만).

설계 원칙:
  - LLM은 **내용 분류·생애주기** 필드만 제안한다. 거버넌스(민감도/PII/접근그룹/owner)는 사람 필수.
  - 출력은 enum으로 제한된 JSON 스키마로 강제한다(자유 텍스트 분류 금지).
  - 각 필드는 confidence를 함께 받고, 임계치 미만이면 UNKNOWN/None으로 남겨 사람이 채우게 한다.
  - 채운 필드는 provenance.auto_filled 에 (필드, confidence, model)로 기록한다.

LLMClient는 Protocol로 주입한다 → 실제 vLLM 없이 단위 테스트 가능(app/clients/llm.py에 실제 구현).
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Protocol

from app.schemas.enums import DocStatus, DocType, Language
from app.schemas.metadata import (
    AutoFilledField,
    ClassificationBlock,
    DocumentMetadata,
    LifecycleBlock,
)

# 이 값 미만 confidence는 채우지 않고 사람 확인으로 넘긴다.
DEFAULT_CONFIDENCE_THRESHOLD = 0.6


class LLMClient(Protocol):
    def complete_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...


def build_enrichment_schema() -> dict[str, Any]:
    """controlled enum으로 제한된 JSON 스키마. 각 필드는 {value, confidence} 형태."""

    def enum_field(values: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string", "enum": values},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["value", "confidence"],
        }

    def free_field() -> dict[str, Any]:
        # title/summary는 분류 enum이 아니라 요약 텍스트 → 자유 텍스트 허용(분류 필드 아님)
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["value", "confidence"],
        }

    return {
        "type": "object",
        "properties": {
            "doc_type": enum_field([e.value for e in DocType]),
            "language": enum_field([e.value for e in Language]),
            "status": enum_field([e.value for e in DocStatus]),
            "title_normalized": free_field(),
            "summary": free_field(),
            "department": free_field(),
            "team": free_field(),
            "topics": {
                "type": "array",
                "items": {"type": "string"},
            },
            "effective_date": free_field(),   # ISO date 문자열 or 빈값
            "expiry_date": free_field(),
            "version": free_field(),
        },
        "required": ["doc_type", "language"],
    }


_PROMPT_TEMPLATE = """당신은 인사 문서의 메타데이터를 분류하는 어시스턴트입니다.
아래 문서 내용을 읽고 주어진 JSON 스키마에 맞춰 메타데이터를 채우세요.

규칙:
- doc_type/language/status 는 반드시 제공된 enum 값 중에서만 고르세요.
- 확신이 없으면 doc_type 은 "unknown", language 는 "unknown" 을 쓰고 confidence 를 낮게 주세요.
- 민감도, 개인정보, 접근권한, 소유자(owner) 는 절대 추론하지 마세요(사람이 채웁니다).
- 날짜는 알 수 없으면 value 를 빈 문자열로 두세요.

파일명: {filename}

문서 내용(일부):
\"\"\"
{content}
\"\"\"
"""

_MAX_CONTENT_CHARS = 6000


def _parse_date(value: str) -> Optional[date]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def enrich(
    doc: DocumentMetadata,
    content: str,
    client: LLMClient,
    model_name: str,
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> DocumentMetadata:
    """LLM 출력으로 classification/lifecycle를 채우고 auto_filled에 기록한다.

    입력 doc은 식별 블록만 채워진 상태를 가정한다. 반환은 갱신된 복사본."""
    schema = build_enrichment_schema()
    filename = doc.identification.source_filename
    prompt = _PROMPT_TEMPLATE.format(
        filename=filename, content=content[:_MAX_CONTENT_CHARS]
    )
    result = client.complete_json(prompt, schema)

    cls = ClassificationBlock()
    life = LifecycleBlock()
    auto: list[AutoFilledField] = []

    def take(field: str) -> Optional[tuple[str, float]]:
        item = result.get(field)
        if not isinstance(item, dict):
            return None
        value = item.get("value")
        conf = float(item.get("confidence", 0.0))
        if value is None or value == "":
            return None
        auto.append(AutoFilledField(field=field, confidence=conf, model=model_name))
        if conf < threshold:
            return None  # 기록은 하되 값은 채우지 않음 → 사람 확인
        return str(value), conf

    if (v := take("doc_type")) is not None:
        try:
            cls.doc_type = DocType(v[0])
        except ValueError:
            pass
    if (v := take("language")) is not None:
        try:
            cls.language = Language(v[0])
        except ValueError:
            pass
    if (v := take("title_normalized")) is not None:
        cls.title_normalized = v[0]
    if (v := take("summary")) is not None:
        cls.summary = v[0]
    if (v := take("department")) is not None:
        cls.department = v[0]
    if (v := take("team")) is not None:
        cls.team = v[0]

    topics = result.get("topics")
    if isinstance(topics, list):
        cls.topics = [str(t) for t in topics if str(t).strip()]

    if (v := take("status")) is not None:
        try:
            life.status = DocStatus(v[0])
        except ValueError:
            pass
    if (v := take("effective_date")) is not None:
        life.effective_date = _parse_date(v[0])
    if (v := take("expiry_date")) is not None:
        life.expiry_date = _parse_date(v[0])
    if (v := take("version")) is not None:
        life.version = v[0]

    doc.classification = cls
    doc.lifecycle = life
    doc.provenance.auto_filled = auto
    return doc
