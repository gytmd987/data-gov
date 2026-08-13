"""적재 실패 원인을 사람이 읽을 수 있게 바꾼다.

업로드가 실패하면 화면에 "AI 처리 중 일시 오류가 발생했습니다"만 떴다. 예외를 통째로
삼켜서 **화면에도 서버 로그에도 원인이 안 남았다** — 관리자조차 무엇이 문제인지 알 수
없었다. 실제 원인은 대개 정해져 있다(AI 서버가 안 떠 있음, 디스크가 꽉 참, 파일이
깨짐…). 그걸 구분해 무엇을 해야 하는지까지 알려 준다.

전체 예외 문자열을 그대로 화면에 뿌리지는 않는다. vLLM/TEI 오류 본문에는 요청에 실린
**문서 본문 일부**가 섞여 나올 수 있어, 남의 문서 내용이 다른 사람 화면에 보일 수 있다.
자세한 내용은 서버 로그로 보내고, 화면에는 분류된 원인만 보여 준다.
"""

from __future__ import annotations

import errno
import re

# vLLM/TEI 클라이언트가 올리는 형태: "vLLM 400 @ http://…: {본문}"
_HTTP_STATUS = re.compile(r"\b(vLLM|TEI \w+)\s+(\d{3})\b")


def explain(exc: BaseException, where: str = "") -> str:
    """예외 → 사람이 읽고 조치할 수 있는 한 줄(문서 내용은 담지 않는다).

    where: 어디서 났는지 아는 경우의 힌트(주소·서비스 이름). 연결 거부 예외는 본문이
    "[Errno 111] Connection refused" 뿐이라 어느 서비스인지 알 수 없다 — 아는 쪽에서
    알려 주면 "AI 서버" 대신 "vLLM(생성 모델)" 처럼 짚어 줄 수 있다.
    """
    name = type(exc).__name__
    text = f"{exc} {where}".strip()

    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "서버 디스크가 가득 찼습니다. 관리자에게 알려 주세요."
    if isinstance(exc, MemoryError):
        return "파일이 너무 커서 서버 메모리가 부족합니다. 나눠서 올려 주세요."

    lowered = text.lower()
    if _looks_like(name, lowered, ("connecterror", "connectionrefused",
                                   "connection refused", "failed to establish")):
        return (f"{_service_of(text)}에 연결하지 못했습니다. "
                "관리자에게 알려 주세요 — 서비스가 떠 있는지 확인이 필요합니다.")
    if _looks_like(name, lowered, ("timeout", "timedout")):
        return (f"{_service_of(text)} 처리 시간이 초과됐습니다. "
                "서버가 바쁠 수 있으니 잠시 후 다시 올려 주세요.")

    status = _HTTP_STATUS.search(text)
    if status:
        service, code = status.group(1), int(status.group(2))
        if code == 400:
            return (f"{service} 가 요청을 거절했습니다(400). 모델 설정이 맞지 않을 수 "
                    "있습니다 — 관리자에게 알려 주세요.")
        if code in (401, 403):
            return f"{service} 인증에 실패했습니다(API 키 확인이 필요합니다)."
        if code == 404:
            return f"{service} 에서 모델을 찾지 못했습니다(모델 이름 확인이 필요합니다)."
        if code >= 500:
            return f"{service} 내부 오류입니다({code}). 잠시 후 다시 올려 주세요."
        return f"{service} 요청이 실패했습니다({code})."

    if "qdrant" in lowered:
        return "검색 저장소(Qdrant)에 문제가 있습니다. 관리자에게 알려 주세요."
    if _looks_like(name, lowered, ("operationalerror", "interfaceerror")):
        return "데이터베이스에 연결하지 못했습니다. 관리자에게 알려 주세요."

    # 분류 못 한 것 — 최소한 예외 종류는 알려 준다(로그에서 찾을 실마리가 된다)
    return f"처리 중 오류가 발생했습니다({name}). 서버 로그를 확인해 주세요."


def _looks_like(name: str, lowered: str, needles: tuple[str, ...]) -> bool:
    haystack = name.lower() + " " + lowered
    return any(n in haystack for n in needles)


def _service_of(text: str) -> str:
    """어느 서비스인지 — 관리자가 어디를 볼지 바로 알 수 있게."""
    lowered = text.lower()
    if "vllm" in lowered or ":8000" in text:
        return "vLLM(생성 모델)"
    if "8081" in text or "embed" in lowered:
        return "TEI 임베딩"
    if "8082" in text or "rerank" in lowered:
        return "TEI 리랭커"
    if "qdrant" in lowered or "6333" in text:
        return "Qdrant"
    return "AI 서버"
