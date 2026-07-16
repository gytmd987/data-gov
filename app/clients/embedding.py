"""TEI 임베딩 클라이언트 (KURE-v1).

pipeline.Embedder 프로토콜을 구현한다. dense 벡터만 생성하며,
하이브리드의 sparse(어휘) 축은 Qdrant 내장 BM25가 담당한다.
"""

from __future__ import annotations

import httpx

from app.config import settings


class TEIEmbedder:
    def __init__(self, base_url: str | None = None, timeout: float = 60.0) -> None:
        self.base_url = (base_url or settings.embedding_url).rstrip("/")
        self._timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(f"{self.base_url}/embed", json={"inputs": texts})
            resp.raise_for_status()
            return resp.json()
