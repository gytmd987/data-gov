"""vLLM 구조화 출력 요청 본문 구성 + JSON 추출 테스트."""

import pytest

from app.clients.llm import build_json_payload, extract_json

SCHEMA = {"type": "object", "properties": {"language": {"type": "string"}}}


def test_guided_json_default_omits_backend():
    p = build_json_payload("q", SCHEMA, "m", mode="guided_json", backend="")
    assert p["guided_json"] == SCHEMA
    assert "guided_decoding_backend" not in p     # 빈 backend는 미전송


def test_guided_json_includes_backend_when_set():
    p = build_json_payload("q", SCHEMA, "m", mode="guided_json", backend="xgrammar")
    assert p["guided_decoding_backend"] == "xgrammar"


def test_response_format_json_schema():
    p = build_json_payload("q", SCHEMA, "m", mode="response_format")
    assert p["response_format"]["type"] == "json_schema"
    assert p["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "guided_json" not in p


def test_json_object_mode_puts_schema_in_prompt():
    p = build_json_payload("질문", SCHEMA, "m", mode="json_object")
    assert p["response_format"] == {"type": "json_object"}
    assert "language" in p["messages"][0]["content"]   # 스키마가 프롬프트에 안내됨


# ── JSON 추출(견고성) ────────────────────────────────────────────────────────
def test_extract_plain_json():
    assert extract_json('{"language": "ko"}') == {"language": "ko"}


def test_extract_from_code_fence():
    text = '```json\n{"doc_type": "policy"}\n```'
    assert extract_json(text) == {"doc_type": "policy"}


def test_extract_after_think_block():
    text = '<think>이 문서는 정책 문서로 보인다</think>\n\n{"doc_type": "policy"}'
    assert extract_json(text) == {"doc_type": "policy"}


def test_extract_with_prose_prefix():
    text = '다음은 결과입니다:\n{"language": "ko", "status": "active"}\n감사합니다.'
    assert extract_json(text) == {"language": "ko", "status": "active"}


def test_extract_single_quoted_dict():
    # 모델이 파이썬 dict 표현(작은따옴표)을 낼 때 복구
    assert extract_json("{'doc_type': 'policy', 'language': 'ko'}") == {
        "doc_type": "policy", "language": "ko"}


def test_extract_trailing_comma():
    assert extract_json('{"language": "ko", "status": "active",}') == {
        "language": "ko", "status": "active"}


def test_extract_single_quoted_in_fence_with_prose():
    text = "결과:\n```json\n{'doc_type': 'payroll'}\n```"
    assert extract_json(text) == {"doc_type": "payroll"}


def test_extract_raises_when_no_json():
    with pytest.raises(ValueError):
        extract_json("JSON이 전혀 없는 응답")
