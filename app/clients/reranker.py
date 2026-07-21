"""TEI 리랭커 클라이언트 (bge-reranker-v2-m3).

search.rerank.Reranker 프로토콜을 구현한다. TEI /rerank 는 {index, score} 목록을
관련도 내림차순으로 반환하므로, 입력 순서에 맞춰 점수 배열로 재정렬한다.
"""

from __future__ import annotations

import httpx

from app.clients.sanitize import clean_texts, strip_surrogates
from app.config import settings


class TEIReranker:
    def __init__(self, base_url: str | None = None, timeout: float = 60.0) -> None:
        self.base_url = (base_url or settings.reranker_url).rstrip("/")
        self._timeout = timeout

    def rerank(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{self.base_url}/rerank",
                json={"query": strip_surrogates(query),
                      "texts": clean_texts(texts), "return_text": False},
            )
            resp.raise_for_status()
            results = resp.json()  # [{"index": i, "score": s}, ...]
        scores = [0.0] * len(texts)
        for item in results:
            scores[item["index"]] = float(item["score"])
        return scores
