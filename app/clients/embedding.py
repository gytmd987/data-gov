"""TEI 임베딩 클라이언트 (KURE-v1).

pipeline.Embedder 프로토콜을 구현한다. dense 벡터만 생성하며,
하이브리드의 sparse(어휘) 축은 Qdrant 내장 BM25가 담당한다.
"""

from __future__ import annotations

import httpx

from app.clients.sanitize import clean_texts
from app.config import settings


class TEIEmbedder:
    def __init__(self, base_url: str | None = None, timeout: float = 60.0) -> None:
        self.base_url = (base_url or settings.embedding_url).rstrip("/")
        self._timeout = timeout

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        clean = [t or " " for t in clean_texts(texts)]
        batch = settings.tei_max_batch  # TEI 최대 배치(기본 32) 초과 시 나눠서 요청
        out: list[list[float]] = []
        with httpx.Client(timeout=self._timeout) as client:
            for start in range(0, len(clean), batch):
                resp = client.post(f"{self.base_url}/embed",
                                   json={"inputs": clean[start:start + batch]})
                if resp.status_code >= 400:
                    raise RuntimeError(f"TEI embed {resp.status_code}: {resp.text[:400]}")
                out.extend(resp.json())
        return out
