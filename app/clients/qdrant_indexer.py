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
        client: QdrantClient | None = None,
    ) -> None:
        self.collection = collection
        self.vector_size = vector_size
        # client 주입 시 그대로 사용(테스트: QdrantClient(location=":memory:")).
        self.client = client or QdrantClient(
            host=host, port=port or settings.qdrant_http_port)

    # 어휘(BM25) 축의 sparse 벡터 이름. Qdrant 가 IDF 를 곱해 준다(우리는 TF 만 보냄).
    SPARSE_NAME = "bm25"

    def ensure_collection(self) -> None:
        from qdrant_client import models as qm
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(
                    size=self.vector_size, distance=Distance.COSINE),
                sparse_vectors_config={
                    self.SPARSE_NAME: qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
            )
            return
        # 기존 컬렉션에 sparse 설정이 없으면 추가한다(재생성 없이 하이브리드 전환).
        try:
            info = self.client.get_collection(self.collection)
            sparse = getattr(info.config.params, "sparse_vectors", None) or {}
            if self.SPARSE_NAME not in sparse:
                self.client.update_collection(
                    collection_name=self.collection,
                    sparse_vectors_config={
                        self.SPARSE_NAME: qm.SparseVectorParams(
                            modifier=qm.Modifier.IDF)})
        except Exception:      # noqa: BLE001 — 구성 확인 실패해도 dense 색인은 계속
            pass

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
        from app.search.lexical import to_qdrant_sparse

        points = []
        for vec, pl, cid in zip(vectors, payloads, ids):
            # dense(의미) + sparse(어휘) 두 축을 같은 포인트에 싣는다.
            vectors_payload = {"": vec,
                               self.SPARSE_NAME: to_qdrant_sparse(pl.get("text", ""))}
            points.append(PointStruct(id=self._point_id(cid),
                                      vector=vectors_payload, payload=pl))
        self.client.upsert(collection_name=self.collection, points=points)

    def _doc_filter(self, parent_doc_id: str):
        from qdrant_client import models as qm
        return qm.Filter(must=[qm.FieldCondition(
            key="parent_doc_id", match=qm.MatchValue(value=parent_doc_id))])

    def set_doc_payload(self, parent_doc_id: str, fields: dict[str, Any]) -> None:
        """한 문서의 모든 청크 payload를 부분 갱신(상태·대체·접근통제 변경 반영).

        문서 관리에서 메타데이터/상태가 바뀌면 이걸로 Qdrant를 동기화해야 검색 하드필터가 맞는다.
        """
        if not self.client.collection_exists(self.collection):
            return
        self.client.set_payload(
            collection_name=self.collection,
            payload=fields,
            points=self._doc_filter(parent_doc_id),
        )

    def delete_doc(self, parent_doc_id: str) -> None:
        """한 문서의 모든 청크 포인트를 Qdrant에서 삭제(하드 삭제)."""
        from qdrant_client import models as qm
        if not self.client.collection_exists(self.collection):
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=qm.FilterSelector(filter=self._doc_filter(parent_doc_id)),
        )
