"""메일 본문에서 **인용된 이전 메일과 서명**을 걷어낸다.

답장은 원문을 통째로 인용해서 온다. 스레드가 5단계면 마지막 메일 하나에 앞의 네 통이
다 들어 있고, 그걸 그대로 색인하면 같은 문장이 다섯 벌 쌓인다. 검색 상위가 같은 내용
으로 채워지고, 어느 문서를 봐야 하는지도 흐려진다.

**메일 사이의 관계는 여기서 잃지 않는다.** 답장·전달 관계는 인용문이 아니라 헤더
(In-Reply-To / References)에 들어 있고, 그건 mailfile.thread_of() 가 따로 읽는다.

규칙은 셋뿐이다.
  ① `>` 로 시작하는 줄      → 그 줄만 버린다
  ② 인용 구분선            → 거기서부터 끝까지 버린다
  ③ 서명 구분자(`--`)      → 거기서부터 끝까지 버린다

지우고 나서 아무것도 안 남으면(인용만 있고 새로 쓴 말이 없는 전달 메일 등) 원문을
그대로 쓴다. 본문이 통째로 사라지는 것보다는 중복이 낫다.
"""

from __future__ import annotations

import re

# ① 인용 부호로 시작하는 줄
_QUOTED = re.compile(r"^\s*>+")

# ② 인용 구분선 — 이 줄부터 아래는 전부 이전 메일이다.
#    메일 클라이언트·언어별로 표기가 달라 흔한 형태를 모아 둔다.
_SEPARATOR = re.compile(
    r"""^\s*(
        -{2,}\s*(original\s+message|forwarded\s+message|원본\s*메시지|전달된\s*메시지)
      | _{5,}\s*$                       # 아웃룩이 넣는 밑줄 구분선
      | -{5,}\s*$
      | (보낸\s*사람|받는\s*사람|보낸사람)\s*:
      | from\s*:\s*.{0,120}?\b(sent|date)\s*:
      | (on\s+.{5,60}\s+wrote\s*:)      # On Mon, Aug 3, 2026 ... wrote:
      | \d{4}[.년/-]\s*\d{1,2}[.월/-]\s*\d{1,2}.{0,60}?(작성|보냄|wrote)\s*:?\s*$
      | .{0,80}?님이\s*(작성|보냄).{0,10}$
    )""",
    re.IGNORECASE | re.VERBOSE)

# ③ 서명 구분자 — 표준은 "-- " 한 줄이다.
_SIGNATURE = re.compile(r"^\s*(--|—|―)\s*$")

# 인용 안내 한 줄만 남기면 문맥이 사라지므로, 이보다 짧아지면 원문을 쓴다.
_MIN_KEPT = 10


def looks_quoted(text: str) -> bool:
    """인용문이 섞여 있는가(잘라낼 게 있는지 미리 보는 용도)."""
    for line in (text or "").splitlines():
        if _QUOTED.match(line) or _SEPARATOR.search(line):
            return True
    return False


def strip_quotes(text: str) -> str:
    """인용문·서명을 걷어낸 본문. 남는 게 없으면 원문을 그대로 돌려준다."""
    original = (text or "").strip()
    if not original:
        return ""

    kept: list[str] = []
    for line in original.splitlines():
        if _SEPARATOR.search(line) or _SIGNATURE.match(line):
            break                       # 여기서부터 끝까지 이전 메일 / 서명
        if _QUOTED.match(line):
            continue                    # 인용 줄만 버린다
        kept.append(line)

    body = "\n".join(kept).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body if len(body) >= _MIN_KEPT else original
