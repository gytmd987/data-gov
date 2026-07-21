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
        # 빈 문자열은 TEI가 거부(422) → 공백으로 대체. 긴 입력은 truncate.
        clean_query = strip_surrogates(query) or " "
        clean = [t or " " for t in clean_texts(texts)]
        batch = settings.tei_max_batch  # TEI 최대 배치(기본 32) 초과 시 나눠서 요청
        scores = [0.0] * len(clean)
        with httpx.Client(timeout=self._timeout) as client:
            for start in range(0, len(clean), batch):
                chunk = clean[start:start + batch]
                resp = client.post(
                    f"{self.base_url}/rerank",
                    json={"query": clean_query, "texts": chunk, "truncate": True},
                )
                if resp.status_code >= 400:
                    raise RuntimeError(f"TEI rerank {resp.status_code}: {resp.text[:400]}")
                for item in resp.json():   # [{"index": i, "score": s}, ...] (배치 내 상대 index)
                    scores[start + item["index"]] = float(item["score"])
        return scores
