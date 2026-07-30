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


class ReadError(Exception):
    """AI 필수 필드(요약/키워드/예상 Q&A)를 채우지 못함 → 파일 읽기 실패로 판단."""


def assert_ai_mandatory(doc) -> None:
    """요약·핵심 키워드·예상 Q&A 가 모두 채워졌는지 확인. 하나라도 비면 ReadError.

    이들은 문서 내용이 실제로 추출됐을 때만 생성 가능하므로, 비어 있으면 파일을
    읽지 못한 것으로 간주한다(스캔 이미지·빈 문서·파싱 실패 등)."""
    cls = doc.classification
    missing = []
    if not (cls.summary and cls.summary.strip()):
        missing.append("요약")
    if not cls.keywords:
        missing.append("핵심 키워드")
    if not cls.expected_qa:
        missing.append("예상 Q&A")
    if missing:
        raise ReadError(
            "파일 내용을 읽지 못했습니다(추출 실패). AI가 다음을 생성하지 못함: "
            + ", ".join(missing) + ". 스캔 문서면 OCR 가능한 형식으로 다시 올려주세요.")


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
            "summary": free_field(),                # [필수] 요약
            "keywords": {                           # [필수] 핵심 키워드(Q&A와 중복 금지)
                "type": "array", "items": {"type": "string"},
            },
            "expected_qa": {                        # [필수] 예상 질의응답
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"question": {"type": "string"},
                                   "answer": {"type": "string"}},
                    "required": ["question", "answer"],
                },
            },
            "related_parties": {                    # 유관 조직/임직원(제안)
                "type": "array", "items": {"type": "string"},
            },
            "references": {                         # 본문이 언급한 다른 문서(제목/파일명)
                "type": "array", "items": {"type": "string"},
            },
            "department": free_field(),
            "effective_date": free_field(),   # ISO date 문자열 or 빈값
            "expiry_date": free_field(),
            "version": free_field(),
        },
        "required": ["doc_type", "language", "summary", "keywords", "expected_qa"],
    }


def doc_type_glossary() -> str:
    """문서 종류 enum 값 + 한글 라벨 + 판별 기준 목록(프롬프트 주입용).

    영문 토큰만 주면 모델이 한국어 문서를 엉뚱한 종류로 넘긴다(예: '설립(안)' 보고서를
    contract 로). 각 값의 뜻을 명시해 판단 근거를 준다. 기준은 config/system.yaml 에서
    수정한다.
    """
    from app import system_config
    hints = system_config.doc_type_hints()
    lines = []
    for value in system_config.doc_types():
        label = system_config.label(value)
        hint = " ".join(str(hints.get(value, "")).split())
        lines.append(f'- "{value}" ({label}){": " + hint if hint else ""}')
    return "\n".join(lines)


_PROMPT_TEMPLATE = """당신은 인사 문서의 메타데이터를 분류하는 어시스턴트입니다.
아래 문서 내용을 읽고 주어진 JSON 스키마에 맞춰 메타데이터를 채우세요.

문서 종류(doc_type) 후보와 판별 기준:
{doc_types}

문서 종류를 고르는 순서:
1. 문서 **제목·표지·머리말에 적힌 문서 유형 표기**를 가장 먼저 보세요
   (예: "○○ 설립(안)" → 내부 검토·보고 문서이므로 report, "○○ 계약서" → contract).
2. 제목에 유형 표기가 없으면 본문의 형식을 보세요
   (조문 형식 → regulation, 당사자·서명란 → contract, 빈 서식 → form).
3. 그래도 애매하면 other 로 두고 confidence 를 낮게 주세요. 억지로 고르지 마세요.
- 흔한 오분류 주의: 회사·조직 **설립/신설/개편 검토 문서는 계약서가 아니라 report** 입니다.
  계약서는 당사자와 서명·계약 조항이 실제로 있는 문서만 해당합니다.

규칙:
- doc_type/language/status 는 반드시 제공된 enum 값 중에서만 고르세요.
- 확신이 없으면 doc_type 은 "unknown", language 는 "unknown" 을 쓰고 confidence 를 낮게 주세요.
- **title_normalized 는 기본적으로 파일명(확장자 제외)을 그대로 쓰세요.**
  단, 파일명이 '새 문서', '무제', 'Document1', '제목없음' 처럼 내용을 알 수 없거나
  오타·깨진 글자가 있으면, 문서 내용에 맞는 제목으로 고쳐서 넣고 confidence 를 높게 주세요.
  파일명이 이미 적절하면 그대로 두세요(내용으로 바꾸지 마세요).
- **summary(요약), keywords(핵심 키워드), expected_qa(예상 질의응답) 는 반드시 채우세요.**
  문서 내용으로 답할 수 있는 실제 질문과 답을 expected_qa 에 3개 이상 만드세요.
  keywords 는 expected_qa 와 겹치지 않는 핵심 용어로만 고르세요(중복 금지).
- 접근권한/작성자/보고선 은 절대 추론하지 마세요(사람이 조직도에서 지정합니다).
- references 에는 본문이 언급하는 **다른 문서의 제목이나 파일명**만 넣으세요(예: "별첨 급여표",
  "「2026 평가 보고서」 참고"). 없으면 빈 배열.
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
        filename=filename, content=content[:_MAX_CONTENT_CHARS],
        doc_types=doc_type_glossary(),
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

    keywords = result.get("keywords")
    if isinstance(keywords, list):
        cls.keywords = [str(t) for t in keywords if str(t).strip()]

    related = result.get("related_parties")
    if isinstance(related, list):
        cls.related_parties = [str(t) for t in related if str(t).strip()]

    refs = result.get("references")
    if isinstance(refs, list):
        cls.references = [str(t) for t in refs if str(t).strip()]

    qa = result.get("expected_qa")
    if isinstance(qa, list):
        cls.expected_qa = [
            {"question": str(x.get("question", "")).strip(),
             "answer": str(x.get("answer", "")).strip()}
            for x in qa if isinstance(x, dict) and x.get("question")]

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
