"""vLLM 스트리밍 응답 파싱 — 조각난 SSE 와 <think> 블록 처리."""

from app.clients.llm import _iter_sse_deltas


def _sse(*pieces: str) -> list[str]:
    """본문 조각들 → vLLM 이 보내는 모양의 SSE 줄."""
    import json
    lines = []
    for p in pieces:
        lines.append("data: " + json.dumps(
            {"choices": [{"delta": {"content": p}}]}, ensure_ascii=False))
        lines.append("")
    lines.append("data: [DONE]")
    return lines


def _run(*pieces: str) -> str:
    return "".join(_iter_sse_deltas(_sse(*pieces)))


def test_joins_plain_deltas():
    assert _run("연차는 ", "15일", "입니다.") == "연차는 15일입니다."


def test_accepts_bytes_lines():
    raw = [line.encode() for line in _sse("연차 ", "15일")]
    assert "".join(_iter_sse_deltas(raw)) == "연차 15일"


def test_ignores_keepalives_and_done_marker():
    lines = ["", ": ping", "data: [DONE]"]
    assert list(_iter_sse_deltas(lines)) == []


def test_skips_malformed_json_without_dying():
    lines = ["data: {not json", *_sse("정상")]
    assert "".join(_iter_sse_deltas(lines)) == "정상"


# ── <think> 블록: 사용자에게 보일 내용이 아니다 ─────────────────────────────
def test_drops_a_think_block():
    assert _run("<think>고민 중</think>", "연차는 15일") == "연차는 15일"


def test_drops_a_think_block_split_across_deltas():
    """태그가 조각 경계에 걸쳐 와도 새면 안 된다 — 실제로 이렇게 쪼개져 온다."""
    assert _run("<thi", "nk>내부 ", "추론</thi", "nk>답변") == "답변"


def test_keeps_text_before_and_after_a_think_block():
    assert _run("앞", "<think>속</think>", "뒤") == "앞뒤"


def test_unclosed_think_block_leaks_nothing():
    assert _run("<think>끝나지 않은 추론") == ""


def test_text_that_merely_resembles_a_tag_is_kept():
    assert _run("부등호 < 는 그대로") == "부등호 < 는 그대로"
