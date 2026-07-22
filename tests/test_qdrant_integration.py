"""Qdrant 통합 테스트 (실제 Qdrant 엔진, in-memory 로컬 모드).

서버·GPU 없이 QdrantClient(location=":memory:")로 색인→검색→하드필터를 실제 엔진으로 검증한다.
임베딩은 결정적 Bag-of-Words fake(TEI 대체)로, 접근통제 배제가 실제 Qdrant 쿼리에서 작동함을 확인.
"""

from datetime import date

import pytest
from qdrant_client import QdrantClient

from app.clients.qdrant_indexer import QdrantIndexer
from app.clients.qdrant_search import QdrantDenseSearch
from app.search.access import AccessPolicy, UserContext
from app.search.retriever import HybridRetriever

VOCAB = ["연차", "휴가", "급여", "평가", "병가", "규정"]


class BowEmbedder:
    """고정 어휘 기반 결정적 임베딩(TEI 대체). 코사인 유사도가 의미를 갖도록."""

    def embed(self, texts):
        vecs = []
        for t in texts:
            v = [float(t.count(w)) for w in VOCAB]
            if not any(v):
                v[0] = 1e-6
            vecs.append(v)
        return vecs


@pytest.fixture
def qdrant():
    return QdrantClient(location=":memory:")


def _index_chunk(indexer, embedder, chunk_id, text, *, groups, title,
                 status="active", expiry=None, superseded=None):
    vec = embedder.embed([text])[0]
    payload = {
        "chunk_id": chunk_id, "parent_doc_id": chunk_id.split("::")[0],
        "access_groups": groups, "status": status,
        "expiry_date": expiry, "superseded_by": superseded,
        "title": title, "page_no": 1, "text": text,
    }
    indexer.upsert(vectors=[vec], payloads=[payload], ids=[chunk_id])


def test_index_and_hard_filtered_retrieval(qdrant):
    embedder = BowEmbedder()
    indexer = QdrantIndexer(collection="hr", vector_size=len(VOCAB), client=qdrant)
    _index_chunk(indexer, embedder, "policy::0", "연차 규정 연차는 15일",
                 groups=["n:2"], title="연차규정")
    _index_chunk(indexer, embedder, "salary::0", "급여 급여 평가 정보",
                 groups=["n:3"], title="급여표")

    dense = QdrantDenseSearch(embedder=embedder, collection="hr", client=qdrant)
    retriever = HybridRetriever(dense=dense)  # dense-only

    # 인사파트(n:2) 사용자: 급여파트(n:3) 문서는 배제되어야
    user = UserContext("u1", frozenset(["n:2"]))
    policy = AccessPolicy.for_user(user, today=date(2026, 7, 20))
    hits = retriever.retrieve("연차 며칠?", policy, top_n=10)
    ids = [h.chunk_id for h in hits]
    assert "policy::0" in ids
    assert "salary::0" not in ids           # 접근통제 하드필터 배제
    # 원문·제목이 payload로 반환됨(답변·인용에 필요)
    top = hits[0]
    assert top.text and top.title == "연차규정"


def test_expired_and_superseded_excluded(qdrant):
    embedder = BowEmbedder()
    indexer = QdrantIndexer(collection="hr", vector_size=len(VOCAB), client=qdrant)
    _index_chunk(indexer, embedder, "cur::0", "연차 규정",
                 groups=["n:2"], title="현행")
    _index_chunk(indexer, embedder, "old::0", "연차 규정 구버전",
                 groups=["n:2"], title="만료", expiry="2020-01-01")
    _index_chunk(indexer, embedder, "sup::0", "연차 규정 대체됨",
                 groups=["n:2"], title="대체", superseded="cur::0")

    dense = QdrantDenseSearch(embedder=embedder, collection="hr", client=qdrant)
    retriever = HybridRetriever(dense=dense)
    user = UserContext("u1", frozenset(["n:2"]))
    policy = AccessPolicy.for_user(user, today=date(2026, 7, 20))
    ids = [h.chunk_id for h in retriever.retrieve("연차", policy, top_n=10)]
    assert ids == ["cur::0"]   # 만료·대체 문서 제외, 현행만
