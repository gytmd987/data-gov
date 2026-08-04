"""어휘 검색(BM25) 토큰화 + 하이브리드 배선.

임베딩이 약한 질의 — 사내 조어, 조항 번호, 문서코드, 숫자, 파일명 — 를 정확 매칭으로
건지는 축이다. 한국어는 조사가 붙으므로 '연차를'과 '연차는'이 겹치는지가 핵심이다.
"""

import pytest

from app.search.lexical import sparse_vector, to_qdrant_sparse, token_id, tokenize


def _toks(text):
    return set(tokenize(text))


# ── 한국어: 조사가 달라도 겹쳐야 한다 ───────────────────────────────────────
@pytest.mark.parametrize("a,b", [
    ("연차를 며칠 쓸 수 있나요", "연차는 15일입니다"),
    ("육아휴직 신청 방법", "육아휴직을 신청하려면"),
    ("경조사 휴가가 궁금해요", "경조사 휴가는 5일이다"),
])
def test_korean_particles_do_not_break_matching(a, b):
    assert _toks(a) & _toks(b), f"공통 토큰이 없다: {a} / {b}"


def test_stem_is_extracted_from_particle():
    assert "연차" in _toks("연차를")
    assert "연차" in _toks("연차는")
    assert "휴가" in _toks("휴가에서")


def test_short_words_are_not_over_stripped():
    """두 글자 이하로 잘려 의미가 사라지는 절단은 하지 않는다."""
    assert "은" not in _toks("은행")     # '은행'을 '행'으로 만들면 안 된다
    assert "은행" in _toks("은행")


# ── 코드·번호·파일명: 정확 매칭 + 부분 매칭 ────────────────────────────────
def test_document_code_matches_whole_and_parts():
    toks = _toks("문서번호 HR-2024-A-017 참고")
    assert "hr-2024-a-017" in toks        # 전체
    assert {"hr", "2024", "017"} <= toks  # 조각으로도 찾을 수 있어야


def test_filename_tokens_include_stem_and_extension():
    toks = _toks("연차규정_최종본.docx")
    assert "연차규정_최종본.docx" in toks
    assert "연차규정" in toks and "docx" in toks


def test_article_number_is_kept_as_one_token():
    assert "제12조" in _toks("제12조(연차 일수)")


def test_amount_and_number_tokens():
    toks = _toks("출장비는 일 150달러를 지급한다")
    assert "150달러" in toks


# ── sparse 벡터 ─────────────────────────────────────────────────────────────
def test_sparse_vector_shape_and_stability():
    idx1, val1 = sparse_vector("연차 휴가 규정")
    idx2, val2 = sparse_vector("연차 휴가 규정")
    assert idx1 == idx2 and val1 == val2      # 색인·질의가 같아야 매칭된다
    assert len(idx1) == len(val1) > 0
    assert idx1 == sorted(idx1)               # 인덱스는 오름차순
    assert all(v > 0 for v in val1)


def test_repeated_terms_get_higher_weight():
    """같은 단어가 여러 번 나오면 가중치가 커진다(sublinear TF)."""
    _, once = sparse_vector("연차")
    _, many = sparse_vector("연차 연차 연차")
    assert max(many) > max(once)


def test_empty_text_gives_empty_vector():
    assert sparse_vector("") == ([], [])
    assert sparse_vector("!!! ???") == ([], [])


def test_token_id_is_deterministic_and_bounded():
    from app.search.lexical import HASH_SPACE
    assert token_id("연차") == token_id("연차")
    assert token_id("연차") != token_id("휴가")
    assert 0 <= token_id("연차") < HASH_SPACE


def test_to_qdrant_sparse_returns_sparse_vector():
    sv = to_qdrant_sparse("연차 휴가")
    assert len(sv.indices) == len(sv.values) > 0


# ── Qdrant 왕복: 색인 → 어휘 검색 ───────────────────────────────────────────
@pytest.fixture
def indexed():
    """인메모리 Qdrant 에 dense+sparse 로 색인한 소규모 코퍼스."""
    from qdrant_client import QdrantClient

    from app.clients.qdrant_indexer import QdrantIndexer
    from app.demo.offline import HashingEmbedder

    client = QdrantClient(location=":memory:")
    emb = HashingEmbedder()
    indexer = QdrantIndexer(vector_size=len(emb.embed(["x"])[0]), client=client)
    docs = {
        "t1": "연차 휴가는 1년 근속 시 15일을 부여하며 미사용분은 수당으로 지급한다.",
        "t2": "제12조(경조사 휴가) 본인 결혼 시 5일의 경조 휴가를 부여한다.",
        "t3": "드림데이 제도는 분기당 1회 자유롭게 쉴 수 있는 사내 제도이다.",
        "t4": "문서번호 HR-2024-A-017 인사평가 운영 기준을 정한다.",
        "n1": "휴가 신청은 사내 시스템에서 결재 상신 후 승인받는다.",
        "n2": "휴직 제도에는 육아휴직, 가족돌봄휴직, 질병휴직이 있다.",
        "n3": "평가 등급은 S, A, B, C, D 다섯 단계로 구분한다.",
    }
    payloads = [{"chunk_id": k, "text": v, "access_groups": ["*"], "status": "active",
                 "parent_doc_id": k} for k, v in docs.items()]
    indexer.upsert(emb.embed(list(docs.values())), payloads, list(docs))
    return client, emb


def test_collection_gets_sparse_vector_config(indexed):
    client, _ = indexed
    info = client.get_collection("hr_chunks")
    assert "bm25" in (info.config.params.sparse_vectors or {})


@pytest.mark.parametrize("query,expected", [
    ("드림데이", "t3"),                    # 임베딩이 모르는 사내 조어
    ("제12조", "t2"),                      # 조항 번호
    ("HR-2024-A-017", "t4"),               # 문서코드
    ("연차를 언제까지 써야 하나요", "t1"),   # 조사가 붙은 한국어
])
def test_lexical_search_finds_exact_terms(indexed, query, expected):
    from app.clients.qdrant_search import QdrantBM25Search
    client, _ = indexed
    hits = QdrantBM25Search(client=client).search_sparse(query, 3, None)
    assert hits and hits[0].payload["chunk_id"] == expected


def test_lexical_search_returns_nothing_for_tokenless_query(indexed):
    from app.clients.qdrant_search import QdrantBM25Search
    client, _ = indexed
    assert QdrantBM25Search(client=client).search_sparse("!!!", 3, None) == []


def test_hybrid_combines_both_axes(indexed):
    """두 축을 RRF 로 융합해도 각 축이 찾던 문서가 살아남는다."""
    from app.clients.qdrant_search import QdrantBM25Search, QdrantDenseSearch
    from app.search.access import AccessPolicy, UserContext
    from app.search.retriever import HybridRetriever

    client, emb = indexed
    retr = HybridRetriever(dense=QdrantDenseSearch(embedder=emb, client=client),
                           sparse=QdrantBM25Search(client=client))
    pol = AccessPolicy.for_user(UserContext(user_id="u", groups=frozenset()))
    for query, expected in [("드림데이", "t3"), ("제12조", "t2")]:
        ids = [c.payload["chunk_id"] for c in retr.retrieve(query, pol, top_n=5)]
        assert expected in ids, f"{query} → {ids}"


# ── 새 검색 축이 권한 구멍이 되면 안 된다 ──────────────────────────────────
def test_lexical_search_respects_access_filter():
    """어휘 검색도 dense 와 똑같이 권한 하드필터를 통과해야 한다."""
    from qdrant_client import QdrantClient

    from app.clients.qdrant_indexer import QdrantIndexer
    from app.clients.qdrant_search import QdrantBM25Search
    from app.demo.offline import HashingEmbedder
    from app.search.access import AccessPolicy, UserContext

    client = QdrantClient(location=":memory:")
    emb = HashingEmbedder()
    indexer = QdrantIndexer(vector_size=len(emb.embed(["x"])[0]), client=client)
    texts = ["드림데이 제도는 분기당 1회 쉬는 사내 제도이다.",
             "드림데이 임원 적용 기준 대외비 문서."]
    payloads = [
        {"chunk_id": "open", "text": texts[0], "access_groups": ["n:2"],
         "status": "active", "parent_doc_id": "d1"},
        {"chunk_id": "secret", "text": texts[1], "access_groups": ["n:99"],
         "status": "active", "parent_doc_id": "d2"},
    ]
    indexer.upsert(emb.embed(texts), payloads, ["open", "secret"])

    user = UserContext(user_id="u", groups=frozenset({"n:2"}))
    pol = AccessPolicy.for_user(user)
    hits = QdrantBM25Search(client=client).search_sparse(
        "드림데이", 5, pol.to_qdrant_filter())
    got = {h.payload["chunk_id"] for h in hits}
    assert got == {"open"}, f"권한 없는 문서가 어휘 검색에 노출됨: {got}"


def test_lexical_search_respects_lifecycle_filter():
    """만료·대체 문서도 dense 와 같은 기준으로 빠져야 한다."""
    from qdrant_client import QdrantClient

    from app.clients.qdrant_indexer import QdrantIndexer
    from app.clients.qdrant_search import QdrantBM25Search
    from app.demo.offline import HashingEmbedder
    from app.search.access import AccessPolicy, UserContext

    client = QdrantClient(location=":memory:")
    emb = HashingEmbedder()
    indexer = QdrantIndexer(vector_size=len(emb.embed(["x"])[0]), client=client)
    texts = ["드림데이 현행 기준.", "드림데이 옛 기준(폐지)."]
    payloads = [
        {"chunk_id": "now", "text": texts[0], "access_groups": ["*"],
         "status": "active", "parent_doc_id": "d1"},
        {"chunk_id": "old", "text": texts[1], "access_groups": ["*"],
         "status": "expired", "parent_doc_id": "d2"},
    ]
    indexer.upsert(emb.embed(texts), payloads, ["now", "old"])

    pol = AccessPolicy.for_user(UserContext(user_id="u", groups=frozenset()))
    hits = QdrantBM25Search(client=client).search_sparse(
        "드림데이", 5, pol.to_qdrant_filter())
    assert {h.payload["chunk_id"] for h in hits} == {"now"}
