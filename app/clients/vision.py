"""vLLM 멀티모달 OCR (이미 서빙 중인 Qwen3.6-27B 재사용).

스캔 PDF·이미지(jpg/png) 파싱 경로의 OCR 콜백(bytes -> markdown text)을 구현한다.
별도 파서(PaddleOCR-VL 등)를 배포하지 않고, 이미 뜬 멀티모달 LLM으로 처리한다.
표가 많은 스캔본·대량 처리로 정확도/처리량이 필요하면 전용 파서로 교체(설정 ocr_backend).
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Optional

import httpx

from app.config import settings

DEFAULT_OCR_PROMPT = (
    "이 문서 이미지를 읽어 내용을 마크다운으로 정확히 옮겨라. "
    "표는 마크다운 표로 재현하고, 숫자·금액은 절대 바꾸지 말고 그대로 옮겨라. "
    "설명·머리말 없이 문서 내용만 출력하라."
)

# (url, json, headers) -> response dict. 테스트에서 주입.
Poster = Callable[[str, dict[str, Any], dict[str, str]], dict[str, Any]]


def detect_mime(image_bytes: bytes) -> str:
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/png"


def build_vision_payload(image_bytes: bytes, model: str, prompt: str) -> dict[str, Any]:
    b64 = base64.b64encode(image_bytes).decode("ascii")
    data_uri = f"data:{detect_mime(image_bytes)};base64,{b64}"
    return {
        "model": model,
        "temperature": 0.0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
    }


class VLLMVisionOCR:
    """OCR 콜백(callable). parsers 의 ocr 인자로 주입한다."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        prompt: str = DEFAULT_OCR_PROMPT,
        timeout: float = 120.0,
        poster: Optional[Poster] = None,
    ) -> None:
        self.base_url = (base_url or settings.vllm_base_url).rstrip("/")
        self.api_key = api_key or settings.vllm_api_key
        self.model = model or settings.vllm_model
        self.prompt = prompt
        self._timeout = timeout
        self._poster = poster

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        if self._poster is not None:
            return self._poster(url, payload, headers)
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def __call__(self, image_bytes: bytes) -> str:
        payload = build_vision_payload(image_bytes, self.model, self.prompt)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        data = self._post(f"{self.base_url}/chat/completions", payload, headers)
        return data["choices"][0]["message"]["content"]
