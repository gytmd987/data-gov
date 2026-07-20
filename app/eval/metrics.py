"""검색 품질 지표 (순위 리스트 기반, 이진 관련성).

모두 순수 함수 → 서비스 없이 단위 테스트 가능.
입력은 '문서 식별자'(예: source_filename) 순위 리스트와 정답 집합.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence


def dedup_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def recall_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    rel = set(relevant)
    if not rel:
        return 0.0
    topk = set(ranked[:k])
    return len(topk & rel) / len(rel)


def reciprocal_rank(ranked: Sequence[str], relevant: Iterable[str]) -> float:
    rel = set(relevant)
    for i, item in enumerate(ranked, start=1):
        if item in rel:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked: Sequence[str], relevant: Iterable[str], k: int) -> float:
    rel = set(relevant)
    if not rel:
        return 0.0
    dcg = 0.0
    for i, item in enumerate(ranked[:k], start=1):
        if item in rel:
            dcg += 1.0 / math.log2(i + 1)
    ideal_n = min(len(rel), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_n + 1))
    return dcg / idcg if idcg > 0 else 0.0
