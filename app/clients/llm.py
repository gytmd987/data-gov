"""vLLM(OpenAI 호환) LLM 클라이언트.

enrichment.LLMClient / answer.TextLLM 프로토콜을 구현한다.
적재 자동채움은 구조화 출력으로 JSON 스키마를 강제한다 → controlled schema 밖 값 차단.
vLLM 버전에 따라 구조화 방식이 달라 settings.vllm_structured_mode 로 선택한다.
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any, Optional

import httpx

from app.config import settings

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"^```[a-zA-Z]*\n?")
_FENCE_CLOSE_RE = re.compile(r"\n?```$")
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _try_parse(s: str) -> Optional[dict[str, Any]]:
    """엄격 JSON → 후행콤마 제거 → 파이썬 리터럴(작은따옴표 dict) 순으로 시도."""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_TRAILING_COMMA_RE.sub(r"\1", s))
    except json.JSONDecodeError:
        pass
    try:
        val = ast.literal_eval(s)   # {'k': 'v'} 같은 파이썬 dict 표현 복구
        if isinstance(val, dict):
            return val
    except (ValueError, SyntaxError):
        pass
    return None


def extract_json(text: str) -> dict[str, Any]:
    """모델 응답에서 JSON 오브젝트를 견고하게 추출한다.

    코드펜스(```json ...```), <think> 추론 블록, 앞뒤 잡텍스트, 작은따옴표/후행콤마까지 견딘다.
    """
    s = _THINK_RE.sub("", text).strip()
    if s.startswith("```"):
        s = _FENCE_CLOSE_RE.sub("", _FENCE_OPEN_RE.sub("", s)).strip()

    # 일부 모델이 중괄호를 이중으로 출력({{ ... }}) → 단일로 정규화
    if s.startswith("{{"):
        s = "{" + s[2:]
    if s.endswith("}}"):
        s = s[:-2] + "}"

    candidates = [s]
    start, end = s.find("{"), s.rfind("}")
    if 0 <= start < end:
        candidates.append(s[start:end + 1])

    for cand in candidates:
        parsed = _try_parse(cand)
        if parsed is not None:
            return parsed
    raise ValueError(f"응답에서 JSON을 찾지 못함: {text[:200]!r}")


def thinking_kwargs(enabled: bool) -> dict[str, Any]:
    """추론(<think>) 사용 여부를 vLLM 에 전달하는 본문 조각.

    Qwen3 계열은 추론이 **기본으로 켜져 있어** "연차 며칠?" 같은 질문에도 수백~수천
    토큰을 먼저 생성한다. 그 토큰은 화면에 안 보이므로(감춘다) 사용자 입장에서는
    그냥 멈춰 있는 시간이다. 도구 선택·SQL 생성처럼 결과가 짧고 정해진 작업에는
    추론이 필요 없다.

    모델이 이 옵션을 모르면 vLLM 이 조용히 무시한다 — 켜 두어도 깨지지 않는다.
    """
    return {"chat_template_kwargs": {"enable_thinking": bool(enabled)}}


def build_json_payload(
    prompt: str, schema: dict[str, Any], model: str,
    mode: str, backend: str = "",
    max_tokens: Optional[int] = None, thinking: bool = False,
) -> dict[str, Any]:
    """구조화 출력 요청 본문을 mode에 맞춰 구성한다(테스트 가능한 순수 함수)."""
    base: dict[str, Any] = {"model": model, "temperature": 0.0}
    if max_tokens:
        base["max_tokens"] = int(max_tokens)
    base.update(thinking_kwargs(thinking))

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
        self.max_tokens = settings.vllm_max_tokens
        self.task_max_tokens = settings.vllm_task_max_tokens
        self._thinking = str(settings.vllm_thinking or "answer").lower()

    def thinks_on(self, kind: str) -> bool:
        """이 종류의 호출에 추론을 쓸 것인가. kind ∈ {answer, task}"""
        if self._thinking == "on":
            return True
        if self._thinking == "off":
            return False
        return kind == "answer"        # "answer"(기본): 답변에만 허용

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

    def complete_json(self, prompt: str, schema: dict[str, Any],
                      retries: int = 2, kind: str = "task",
                      max_tokens: Optional[int] = None) -> dict[str, Any]:
        """구조화 출력 요청. 모델이 간혹 JSON이 아닌 응답을 내므로 몇 회 재시도한다.

        (같은 파일을 다시 올리면 됐던 이유 = 이 응답이 비결정적이기 때문. 이제 자동 재시도한다.)

        kind="task" 가 기본이다 — 도구 선택·SQL 생성처럼 **결과가 짧고 정해진** 호출이라
        추론(<think>)이 필요 없다. 적재 자동채움처럼 판단이 필요한 곳은 kind="answer".
        """
        payload = build_json_payload(
            prompt, schema, self.model, self.structured_mode, self.guided_backend,
            max_tokens=max_tokens or self.task_max_tokens,
            thinking=self.thinks_on(kind))
        last_err: Exception | None = None
        for _ in range(max(1, retries + 1)):
            data = self._post_chat(payload)
            content = data["choices"][0]["message"]["content"]
            try:
                return extract_json(content)
            except ValueError as e:
                last_err = e   # 비-JSON 응답 → 재시도
        raise last_err  # type: ignore[misc]

    def complete_text(self, prompt: str, temperature: float = 0.2) -> str:
        """일반 텍스트 생성(답변 생성용). answer.TextLLM 프로토콜 구현."""
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": self.max_tokens,
            **thinking_kwargs(self.thinks_on("answer")),
        }
        data = self._post_chat(payload)
        return data["choices"][0]["message"]["content"]

    def stream_text(self, prompt: str, temperature: float = 0.2):
        """답변을 토큰 단위로 흘려보낸다(생성기).

        답변 생성은 4~9초가 걸리는데, 다 만든 뒤 한 번에 주면 그동안 화면이 멈춰
        보인다. 첫 글자부터 내보내면 총 시간이 같아도 훨씬 빠르게 느껴진다.

        `<think>` 추론 블록은 걸러 낸다 — 사용자에게 보일 내용이 아니다.
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
            **thinking_kwargs(self.thinks_on("answer")),
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = f"{self.base_url}/chat/completions"
        with httpx.Client(timeout=self._timeout) as client:
            with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise RuntimeError(
                        f"vLLM {resp.status_code} @ {url}: {resp.text[:500]}")
                yield from _iter_sse_deltas(resp.iter_lines())


def _iter_sse_deltas(lines):
    """vLLM 의 SSE 응답에서 본문 조각만 뽑아 낸다(`<think>` 블록 제외)."""
    in_think = False
    buffer = ""
    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            chunk = json.loads(body)
        except json.JSONDecodeError:
            continue
        piece = (chunk.get("choices") or [{}])[0].get("delta", {}).get("content")
        if not piece:
            continue
        # <think> 태그가 조각 경계에 걸쳐 올 수 있어 버퍼에 모아 처리한다
        buffer += piece
        while buffer:
            if in_think:
                end = buffer.find("</think>")
                if end < 0:
                    buffer = buffer[-8:] if len(buffer) > 8 else buffer
                    break
                buffer = buffer[end + len("</think>"):]
                in_think = False
                continue
            start = buffer.find("<think>")
            if start < 0:
                # 태그가 반쯤 걸쳐 있을 수 있으니 끝부분은 남겨 둔다
                keep = 0
                for n in range(min(7, len(buffer)), 0, -1):
                    if "<think>".startswith(buffer[-n:]):
                        keep = n
                        break
                out, buffer = buffer[:len(buffer) - keep], buffer[len(buffer) - keep:]
                if out:
                    yield out
                break
            out, buffer = buffer[:start], buffer[start + len("<think>"):]
            in_think = True
            if out:
                yield out
    if buffer and not in_think:
        yield buffer
