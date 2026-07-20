"""vLLM 멀티모달 OCR 콜백 테스트 (서버 없이 poster 주입)."""

from app.clients.vision import VLLMVisionOCR, build_vision_payload, detect_mime

# 최소 PNG/JPEG 매직바이트
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 8


def test_detect_mime():
    assert detect_mime(PNG) == "image/png"
    assert detect_mime(JPG) == "image/jpeg"


def test_build_vision_payload_has_image_and_text():
    payload = build_vision_payload(JPG, model="qwen", prompt="읽어라")
    content = payload["messages"][0]["content"]
    kinds = {c["type"] for c in content}
    assert kinds == {"text", "image_url"}
    img = next(c for c in content if c["type"] == "image_url")
    assert img["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert payload["temperature"] == 0.0


def test_ocr_callable_returns_model_text():
    captured = {}

    def fake_poster(url, payload, headers):
        captured["url"] = url
        captured["payload"] = payload
        captured["auth"] = headers.get("Authorization")
        return {"choices": [{"message": {"content": "연차는 15일 [표 인식됨]"}}]}

    ocr = VLLMVisionOCR(base_url="http://vllm/v1", api_key="k", model="qwen",
                        poster=fake_poster)
    text = ocr(PNG)   # OCRFn 시그니처: bytes -> str

    assert text == "연차는 15일 [표 인식됨]"
    assert captured["url"] == "http://vllm/v1/chat/completions"
    assert captured["auth"] == "Bearer k"
    # 이미지가 data URI로 실려 나감
    content = captured["payload"]["messages"][0]["content"]
    assert any(c["type"] == "image_url" for c in content)
