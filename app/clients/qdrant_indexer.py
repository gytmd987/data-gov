"""Qdrant 인덱서.

pipeline.Indexer 프로토콜을 구현한다. 청크 벡터 + payload(접근통제/생애주기 상속)를 업서트한다.
payload의 access_groups/sensitivity_rank/status/expiry_date/superseded_by 는
검색 시 하드 필터링(권한/민감도/만료/대체)에 사용된다.
"""

from __future__ import annotations

import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from app.config import settings


class QdrantIndexer:
    def __init__(
        self,
        collection: str = "hr_chunks",
        vector_size: int = 1024,   # KURE-v1(BGE-M3 계열) dense 차원
        host: str = "localhost",
        port: int | None = None,
    ) -> None:
        self.collection = collection
        self.vector_size = vector_size
        self.client = QdrantClient(host=host, port=port or settings.qdrant_http_port)

    def ensure_collection(self) -> None:
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(
                    size=self.vector_size, distance=Distance.COSINE),
            )

    @staticmethod
    def _point_id(chunk_id: str) -> str:
        # Qdrant 포인트 ID는 UUID/정수. chunk_id 문자열을 안정적 UUID로 변환.
        return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))

    def upsert(
        self,
        vectors: list[list[float]],
        payloads: list[dict[str, Any]],
        ids: list[str],
    ) -> None:
        self.ensure_collection()
        points = [
            PointStruct(id=self._point_id(cid), vector=vec, payload=pl)
            for vec, pl, cid in zip(vectors, payloads, ids)
        ]
        self.client.upsert(collection_name=self.collection, points=points)
