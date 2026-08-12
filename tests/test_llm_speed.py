"""응답 속도 관련 요청 옵션 — 생성 상한과 추론(<think>) 제어.

생성 토큰 수가 곧 대기 시간이다. 상한이 없으면 모델이 늘어놓는 만큼 사용자가 기다리고,
추론이 켜져 있으면 **화면에 안 보이는** 토큰을 수백~수천 개 먼저 만든다.
"""

import pytest

from app.clients.llm import VLLMClient, build_json_payload, thinking_kwargs

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}}


def _client(monkeypatch, **overrides):
    from app.config import settings
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)
    return VLLMClient(base_url="http://x/v1", api_key="k", model="m")


# ── 추론 스위치 ──────────────────────────────────────────────────────────────
def test_thinking_kwargs_shape():
    assert thinking_kwargs(False) == {"chat_template_kwargs": {"enable_thinking": False}}
    assert thinking_kwargs(True) == {"chat_template_kwargs": {"enable_thinking": True}}


@pytest.mark.parametrize("mode,answer,task", [
    ("off", False, False),        # 제일 빠름
    ("answer", True, False),      # 기본 — 도구 선택·SQL 은 추론 불필요
    ("on", True, True),           # 예전 동작
])
def test_thinking_mode_decides_per_call_kind(monkeypatch, mode, answer, task):
    client = _client(monkeypatch, vllm_thinking=mode)
    assert client.thinks_on("answer") is answer
    assert client.thinks_on("task") is task


def test_unknown_thinking_mode_falls_back_to_the_default(monkeypatch):
    """설정 오타 때문에 추론이 전부 켜지거나 꺼지면 안 된다."""
    client = _client(monkeypatch, vllm_thinking="아무거나")
    assert client.thinks_on("answer") is True and client.thinks_on("task") is False


# ── 생성 상한 ────────────────────────────────────────────────────────────────
def test_chat_payload_carries_a_token_limit_and_thinking(monkeypatch):
    client = _client(monkeypatch, vllm_max_tokens=800, vllm_thinking="answer")
    sent = {}
    monkeypatch.setattr(client, "_post_chat",
                        lambda p: sent.update(p) or
                        {"choices": [{"message": {"content": "답"}}]})

    client.complete_text("안녕")

    assert sent["max_tokens"] == 800
    assert sent["chat_template_kwargs"] == {"enable_thinking": True}


def test_structured_calls_are_short_and_skip_thinking_by_default(monkeypatch):
    """도구 선택·SQL 은 결과가 짧고 정해져 있다 — 추론은 시간만 쓴다."""
    client = _client(monkeypatch, vllm_task_max_tokens=400, vllm_thinking="answer")
    sent = {}
    monkeypatch.setattr(client, "_post_chat",
                        lambda p: sent.update(p) or
                        {"choices": [{"message": {"content": '{"a":"b"}'}}]})

    client.complete_json("무엇을 할까", SCHEMA)

    assert sent["max_tokens"] == 400
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


def test_ingestion_can_ask_for_thinking_and_more_room(monkeypatch):
    """적재 자동채움은 야간 배치라 품질이 우선 — 추론을 켜고 넉넉히 준다."""
    client = _client(monkeypatch, vllm_thinking="answer")
    sent = {}
    monkeypatch.setattr(client, "_post_chat",
                        lambda p: sent.update(p) or
                        {"choices": [{"message": {"content": '{"a":"b"}'}}]})

    client.complete_json("문서 분류", SCHEMA, kind="answer", max_tokens=2000)

    assert sent["max_tokens"] == 2000
    assert sent["chat_template_kwargs"] == {"enable_thinking": True}


def test_streaming_payload_also_bounded(monkeypatch):
    client = _client(monkeypatch, vllm_max_tokens=500)
    assert client.max_tokens == 500      # stream_text 가 이 값을 싣는다


@pytest.mark.parametrize("mode", ["guided_json", "response_format", "json_object"])
def test_every_structured_mode_carries_the_limit(mode):
    payload = build_json_payload("p", SCHEMA, "m", mode, max_tokens=300, thinking=False)
    assert payload["max_tokens"] == 300
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_no_limit_means_the_field_is_omitted():
    """0/None 이면 아예 안 보낸다 — 서버 기본값을 쓰라는 뜻."""
    assert "max_tokens" not in build_json_payload("p", SCHEMA, "m", "guided_json")
