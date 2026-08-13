"""적재 실패 원인 분류 — 화면에 "일시 오류"만 뜨고 로그에도 안 남던 문제.

원인을 알려 주되 **문서 본문은 흘리지 않는다.** vLLM/TEI 오류 본문에는 요청에 실린
문서 조각이 섞여 나올 수 있어, 그대로 뿌리면 남의 문서가 다른 사람 화면에 보인다.
"""

import errno

import httpx
import pytest

from app.ingestion.failures import explain

SECRET = "홍길동 연봉 8,500만원 인사평가 S등급"


# ── 서버가 안 떠 있음 (제일 흔한 원인) ──────────────────────────────────────
def test_connection_refused_names_the_service_and_what_to_do():
    out = explain(httpx.ConnectError("[Errno 111] Connection refused"))
    assert "연결하지 못했습니다" in out and "관리자" in out


@pytest.mark.parametrize("text,expected", [
    ("vLLM 연결 실패 @ http://localhost:8000/v1", "vLLM"),
    ("TEI embed @ http://localhost:8081", "임베딩"),
    ("rerank 실패 http://localhost:8082", "리랭커"),
    ("qdrant unavailable", "Qdrant"),
])
def test_says_which_service_is_down(text, expected):
    assert expected in explain(httpx.ConnectError(text))


def test_timeout_suggests_retrying():
    out = explain(httpx.ReadTimeout("timed out"))
    assert "시간이 초과" in out and "다시" in out


# ── 서버는 떴는데 거절함 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("status,needle", [
    (400, "거절"), (401, "인증"), (403, "인증"), (404, "모델"), (500, "내부 오류"),
])
def test_http_status_is_translated(status, needle):
    out = explain(RuntimeError(f"vLLM {status} @ http://x/v1: {{}}"))
    assert needle in out


def test_error_body_never_reaches_the_message():
    """vLLM 오류 본문에 문서 내용이 섞여 와도 화면에 나가면 안 된다."""
    out = explain(RuntimeError(f"vLLM 400 @ http://x/v1: {{'prompt': '{SECRET}'}}"))
    assert SECRET not in out
    assert "홍길동" not in out and "8,500" not in out


def test_tei_status_is_translated_too():
    assert "500" in explain(RuntimeError(f"TEI embed 500: {SECRET}"))
    assert SECRET not in explain(RuntimeError(f"TEI embed 500: {SECRET}"))


# ── 서버 자원 ────────────────────────────────────────────────────────────────
def test_disk_full_is_called_out_plainly():
    exc = OSError(errno.ENOSPC, "No space left on device")
    out = explain(exc)
    assert "디스크" in out and "가득" in out


def test_other_os_errors_are_not_mistaken_for_disk_full():
    out = explain(OSError(errno.EACCES, "Permission denied"))
    assert "디스크" not in out


def test_out_of_memory_suggests_splitting_the_upload():
    assert "메모리" in explain(MemoryError()) and "나눠서" in explain(MemoryError())


# ── 그 밖 ────────────────────────────────────────────────────────────────────
def test_database_failure_is_recognised():
    class OperationalError(Exception):
        pass

    assert "데이터베이스" in explain(OperationalError("could not connect"))


def test_unknown_error_still_names_the_exception_type():
    """분류 못 해도 종류는 알려 줘야 로그에서 찾을 실마리가 된다."""
    class WeirdParserError(Exception):
        pass

    out = explain(WeirdParserError(SECRET))
    assert "WeirdParserError" in out
    assert SECRET not in out, "분류 못 한 예외의 본문이 새어 나갔다"


def test_message_is_one_short_line():
    """말풍선 한 줄에 들어가야 한다 — 스택트레이스를 화면에 뿌리지 않는다."""
    out = explain(RuntimeError("x" * 5000))
    assert len(out) < 200 and "\n" not in out


# ── where 힌트 ───────────────────────────────────────────────────────────────
def test_hint_names_the_service_when_the_exception_cannot():
    """연결 거부 예외는 본문이 'Connection refused' 뿐이라 어디인지 알 수 없다."""
    bare = httpx.ConnectError("[Errno 111] Connection refused")

    assert "AI 서버에" in explain(bare)                       # 힌트 없으면 뭉뚱그림
    assert "vLLM" in explain(bare, "http://localhost:8000/v1")
    assert "리랭커" in explain(bare, "http://localhost:8082")


def test_hint_does_not_override_what_the_exception_already_says():
    out = explain(RuntimeError("vLLM 500 @ http://x/v1: boom"), "http://localhost:8082")
    assert "500" in out and "내부 오류" in out
