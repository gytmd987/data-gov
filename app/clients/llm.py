"""vLLM(OpenAI 호환) LLM 클라이언트.

enrichment.LLMClient / answer.TextLLM 프로토콜을 구현한다.
적재 자동채움은 구조화 출력으로 JSON 스키마를 강제한다 → controlled schema 밖 값 차단.
vLLM 버전에 따라 구조화 방식이 달라 settings.vllm_structured_mode 로 선택한다.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import settings


def build_json_payload(
    prompt: str, schema: dict[str, Any], model: str,
    mode: str, backend: str = "",
) -> dict[str, Any]:
    """구조화 출력 요청 본문을 mode에 맞춰 구성한다(테스트 가능한 순수 함수)."""
    base = {"model": model, "temperature": 0.0}

    if mode == "response_format":
        # OpenAI 표준 json_schema (최신 vLLM 권장)
        base["messages"] = [{"role": "user", "content": prompt}]
        base["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "metadata", "schema": schema},
        }
    elif mode == "json_object":
        # 스키마 강제는 약함 → 프롬프트에 스키마를 안내하고 JSON 오브젝트만 받는다
        guide = prompt + "\n\n다음 JSON 스키마에 맞춰 JSON만 출력:\n" + json.dumps(
            schema, ensure_ascii=False)
        base["messages"] = [{"role": "user", "content": guide}]
        base["response_format"] = {"type": "json_object"}
    else:  # "guided_json" (기본, vLLM 확장)
        base["messages"] = [{"role": "user", "content": prompt}]
        base["guided_json"] = schema
        if backend:  # 빈 값이면 미전송 → vLLM 기본 백엔드 사용
            base["guided_decoding_backend"] = backend

    return base


class VLLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
        structured_mode: str | None = None,
        guided_backend: str | None = None,
    ) -> None:
        self.base_url = (base_url or settings.vllm_base_url).rstrip("/")
        self.api_key = api_key or settings.vllm_api_key
        self.model = model or settings.vllm_model
        self._timeout = timeout
        self.structured_mode = structured_mode or settings.vllm_structured_mode
        self.guided_backend = (
            guided_backend if guided_backend is not None else settings.vllm_guided_backend)

    def _post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            if resp.status_code >= 400:
                # 진단 편의를 위해 응답 본문을 오류에 포함(400 사유 등)
                raise RuntimeError(
                    f"vLLM {resp.status_code} @ {url}: {resp.text[:500]}")
            return resp.json()

    def complete_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        payload = build_json_payload(
            prompt, schema, self.model, self.structured_mode, self.guided_backend)
        data = self._post_chat(payload)
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)

    def complete_text(self, prompt: str, temperature: float = 0.2) -> str:
        """일반 텍스트 생성(답변 생성용). answer.TextLLM 프로토콜 구현."""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
        }
        data = self._post_chat(payload)
        return data["choices"][0]["message"]["content"]
