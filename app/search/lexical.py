"""어휘 검색(BM25)용 토큰화 + sparse 벡터.

임베딩(dense)이 약한 곳 — 사내 조어("드림데이"), 조항 번호("제12조"), 숫자·금액,
사번·문서코드, 원본 파일명 — 을 정확 매칭으로 건지기 위한 축이다.

**왜 토큰화를 직접 하나**
Qdrant 내장 BM25(`Qdrant/bm25`)는 fastembed 모델을 내려받아야 하고(폐쇄망 부담),
공백 기준이라 한국어 조사를 처리하지 못한다("연차를"과 "연차는"이 다른 토큰).
그래서 여기서 토큰만 만들고, **IDF 가중치는 Qdrant 가 서버에서 계산**하게 한다
(컬렉션의 sparse 벡터에 `Modifier.IDF` 설정). 우리는 TF 만 보내면 된다.

**토큰화 방식**
1. 어절을 정규화해 그대로 담는다(영문·숫자·코드는 이걸로 정확 매칭된다)
2. 한글 어절은 **음절 bigram** 도 함께 담는다 → 조사가 붙어도 겹친다
   ("연차를" → 연차를, 연차, 차를 / "연차는" → 연차는, 연차, 차는 → '연차'가 공통)
3. 흔한 조사·어미로 끝나면 그 꼬리를 뗀 형태도 추가한다(가벼운 어간 추출)
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable

# sparse 벡터 인덱스 공간(해시 충돌 확률을 낮추려면 크게. 메모리는 실제 토큰 수만 씀)
HASH_SPACE = 2 ** 20

_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]+(?:[-_./][0-9A-Za-z가-힣]+)*")
_SEP_RE = re.compile(r"[-_./]")
_HANGUL_RE = re.compile(r"[가-힣]")

# 한국어에서 아주 흔한 조사·어미. 어절 끝에서만 떼어 본다(가벼운 어간 추출).
_TAILS = ("으로써", "에서는", "에게서", "이라고", "하여야", "으로는", "에서의",
          "으로", "에서", "에게", "께서", "이나", "라도", "부터", "까지", "마다",
          "처럼", "보다", "만큼", "한테", "이란", "라는", "이며", "하고", "와의",
          "과의", "의", "은", "는", "이", "가", "을", "를", "에", "도", "만",
          "및", "와", "과", "로", "라", "게", "함", "됨", "임")

MIN_STEM_LEN = 2      # 꼬리를 뗀 뒤 이보다 짧아지면 버린다(과도한 절단 방지)


def _strip_tail(word: str) -> str | None:
    """어절 끝의 조사/어미를 한 번 떼어 본다. 뗄 게 없으면 None."""
    if not _HANGUL_RE.search(word):
        return None
    for tail in _TAILS:
        if word.endswith(tail) and len(word) - len(tail) >= MIN_STEM_LEN:
            return word[: -len(tail)]
    return None


def _bigrams(word: str) -> list[str]:
    return [word[i:i + 2] for i in range(len(word) - 1)]


def tokenize(text: str) -> list[str]:
    """검색 토큰 목록(중복 포함 — 빈도가 곧 가중치가 된다)."""
    tokens: list[str] = []
    for raw in _WORD_RE.findall((text or "").lower()):
        tokens.append(raw)
        # 코드형 토큰은 조각도 담는다: 'hr-2024-a-017.docx' → hr, 2024, a, 017, docx
        # (파일명 전체를 몰라도 일부만으로 찾을 수 있어야 한다)
        if _SEP_RE.search(raw):
            tokens.extend(p for p in _SEP_RE.split(raw) if p)
        stem = _strip_tail(raw)
        if stem:
            tokens.append(stem)
        if _HANGUL_RE.search(raw) and len(raw) > 2:
            tokens.extend(_bigrams(raw))     # 조사가 붙어도 겹치도록
    return tokens


def token_id(token: str) -> int:
    """토큰 → sparse 인덱스(안정적 해시). 색인·질의가 같은 함수를 써야 한다."""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % HASH_SPACE


def sparse_vector(text: str) -> tuple[list[int], list[float]]:
    """(indices, values) — 값은 sublinear TF(1+log tf). IDF 는 Qdrant 가 곱한다."""
    counts: dict[int, int] = {}
    for tok in tokenize(text):
        idx = token_id(tok)
        counts[idx] = counts.get(idx, 0) + 1
    if not counts:
        return [], []
    items = sorted(counts.items())
    indices = [i for i, _ in items]
    values = [1.0 + math.log(c) for _, c in items]
    return indices, values


def to_qdrant_sparse(text: str):
    """qdrant_client SparseVector 로 변환(런타임 import 로 의존 격리)."""
    from qdrant_client import models as qm

    indices, values = sparse_vector(text)
    return qm.SparseVector(indices=indices, values=values)


def has_tokens(text: str) -> bool:
    return bool(tokenize(text))


def overlap(a: str, b: str) -> Iterable[str]:
    """디버깅용: 두 텍스트의 공통 토큰."""
    return set(tokenize(a)) & set(tokenize(b))
