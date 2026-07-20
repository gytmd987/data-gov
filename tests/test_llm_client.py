"""vLLM 구조화 출력 요청 본문 구성 테스트."""

from app.clients.llm import build_json_payload

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
