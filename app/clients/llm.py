"""vLLM(OpenAI 호환) LLM 클라이언트.

enrichment.LLMClient 프로토콜을 구현한다. vLLM의 guided decoding(guided_json)으로
출력을 JSON 스키마에 강제한다 → controlled schema 밖 값 생성 차단.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import settings


class VLLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = (base_url or settings.vllm_base_url).rstrip("/")
        self.api_key = api_key or settings.vllm_api_key
        self.model = model or settings.vllm_model
        self._timeout = timeout

    def complete_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            # vLLM 확장: 출력 JSON을 스키마에 강제
            "guided_json": schema,
            "guided_decoding_backend": "outlines",
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)
