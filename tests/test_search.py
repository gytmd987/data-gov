"""검색·답변 파이프라인 테스트 (외부 서비스 없이 fake 주입)."""

from datetime import date

from app.search.access import AccessPolicy, UserContext
from app.search.answer import build_answer_prompt, generate_answer, parse_citations
from app.search.fusion import reciprocal_rank_fusion
from app.search.pipeline import SearchPipeline
from app.search.rerank import rerank_chunks
from app.search.retriever import HybridRetriever
from app.search.types import RetrievedChunk


def _chunk(cid, text="본문", *, groups=("n:1",), status="active",
           expiry=None, superseded=None, title="문서", page=1, score=0.0):
    return RetrievedChunk(
        chunk_id=cid, text=text, score=score,
        payload={
            "chunk_id": cid, "parent_doc_id": cid.split("::")[0],
            "access_groups": list(groups),
            "status": status, "expiry_date": expiry, "superseded_by": superseded,
            "title": title, "page_no": page,
        })


def _user(groups=("n:1",)):
    # groups = 조직 접근 토큰(n:{노드}, h:{노드})
    return UserContext(user_id="u1", groups=frozenset(groups))


# ── 접근통제 allows() (조직 토큰) ────────────────────────────────────────────
def test_allows_permits_matching_token():
    pol = AccessPolicy.for_user(_user(), today=date(2026, 7, 16))
    assert pol.allows(_chunk("d::0").payload)


def test_allows_denies_token_mismatch():
    pol = AccessPolicy.for_user(_user(groups=("n:2",)), today=date(2026, 7, 16))
    assert not pol.allows(_chunk("d::0", groups=("n:1",)).payload)


def test_allows_head_token_matches_head_scoped_doc():
    # 부서장(h:1)만 열람 가능한 문서 → 파트원(n:1)은 불가, 부서장(h:1)은 가능
    doc = _chunk("d::0", groups=("h:1",)).payload
    assert not AccessPolicy.for_user(_user(groups=("n:1",))).allows(doc)
    assert AccessPolicy.for_user(_user(groups=("n:1", "h:1"))).allows(doc)


def test_allows_active_and_archived_but_denies_expired_superseded_draft():
    pol = AccessPolicy.for_user(_user(), today=date(2026, 7, 16))
    # 유효·보관은 기본 검색 노출
    assert pol.allows(_chunk("d::0", status="active").payload)
    assert pol.allows(_chunk("d::0", status="archived").payload)
    # 초안·만료·대체는 제외
    assert not pol.allows(_chunk("d::0", status="draft").payload)
    assert not pol.allows(_chunk("d::0", expiry="2020-01-01").payload)
    assert not pol.allows(_chunk("d::0", superseded="newdoc").payload)


def test_allows_wildcard_open_to_all():
    # "*" = 팀 전체 공개 — 토큰이 전혀 안 겹쳐도 통과
    pol = AccessPolicy.for_user(_user(groups=("n:2",)), today=date(2026, 7, 16))
    assert pol.allows(_chunk("d::0", groups=("*",)).payload)


def test_to_qdrant_filter_builds():
    pol = AccessPolicy.for_user(_user())
    f = pol.to_qdrant_filter()
    # must 조건 2개(access_groups/status)
    assert len(f.must) == 2
    # 그룹 필터에 팀 전체 센티널 "*" 포함(공개 문서도 후보에 들어오도록)
    assert "*" in f.must[0].match.any


# ── 과거 문서 포함(include_past) ──────────────────────────────────────────────
def test_include_past_permits_expired_superseded_archived():
    pol = AccessPolicy.for_user(_user(), today=date(2026, 7, 16), include_past=True)
    assert pol.allows(_chunk("d::0", status="archived").payload)
    assert pol.allows(_chunk("d::0", expiry="2020-01-01").payload)
    assert pol.allows(_chunk("d::0", superseded="newdoc").payload)


def test_include_past_still_enforces_tokens():
    # 과거 포함이어도 토큰 불일치는 거부
    pol = AccessPolicy.for_user(_user(groups=("n:2",)),
                                today=date(2026, 7, 16), include_past=True)
    assert not pol.allows(_chunk("d::0", groups=("n:1",), status="expired").payload)


def test_to_qdrant_filter_include_past_drops_status():
    pol = AccessPolicy.for_user(_user(), include_past=True)
    f = pol.to_qdrant_filter()
    # status==active 조건이 빠져 access_groups 1개만 남음
    assert len(f.must) == 1


# ── RRF 융합 ─────────────────────────────────────────────────────────────────
def test_rrf_merges_and_dedups():
    dense = [_chunk("a"), _chunk("b"), _chunk("c")]
    sparse = [_chunk("b"), _chunk("d")]
    fused = reciprocal_rank_fusion([dense, sparse])
    ids = [c.chunk_id for c in fused]
    assert set(ids) == {"a", "b", "c", "d"}
    assert ids[0] == "b"   # 양쪽에 등장 → 최상위


# ── 리랭킹 ───────────────────────────────────────────────────────────────────
class FakeReranker:
    def __init__(self, scores):
        self.scores = scores

    def rerank(self, query, texts):
        return self.scores[: len(texts)]


def test_rerank_reorders_and_truncates():
    chunks = [_chunk("a"), _chunk("b"), _chunk("c")]
    out = rerank_chunks(FakeReranker([0.1, 0.9, 0.5]), "q", chunks, top_k=2)
    assert [c.chunk_id for c in out] == ["b", "c"]


# ── 답변/인용 ────────────────────────────────────────────────────────────────
def test_build_prompt_numbers_context():
    chunks = [_chunk("a", "연차 15일", title="연차규정", page=2)]
    prompt = build_answer_prompt("연차 며칠?", chunks)
    assert "[1]" in prompt and "연차규정" in prompt and "p.2" in prompt


def test_parse_citations_extracts_markers():
    chunks = [_chunk("a", title="규정A"), _chunk("b", title="규정B")]
    cites = parse_citations("답 [1] 그리고 [2]. 중복 [1].", chunks)
    assert [c.marker for c in cites] == [1, 2]
    assert cites[0].title == "규정A"


class FakeLLM:
    def __init__(self, text):
        self.text = text

    def complete_text(self, prompt):
        return self.text


def test_generate_answer_no_context():
    ans = generate_answer(FakeLLM("무관"), "q", [])
    assert "확인할 수 없습니다" in ans.text
    assert ans.citations == []


# ── 전체 파이프라인 + 하드필터 후처리 + 감사 ────────────────────────────────
class FakeDense:
    def __init__(self, chunks):
        self.chunks = chunks

    def search_dense(self, query, top_n, qdrant_filter):
        return self.chunks


class FakeAudit:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


def test_pipeline_filters_restricted_and_audits():
    # 허용 청크 + 다른 조직(n:2) 청크를 dense가 함께 반환 → 후처리에서 배제되어야
    allowed = _chunk("ok::0", "연차는 15일 [approved]", groups=("n:1",), title="연차규정")
    restricted = _chunk("secret::0", "급여 정보", groups=("n:2",), title="급여표")
    retriever = HybridRetriever(dense=FakeDense([allowed, restricted]))
    reranker = FakeReranker([0.9, 0.8])
    audit = FakeAudit()
    pipe = SearchPipeline(
        retriever=retriever, reranker=reranker,
        llm=FakeLLM("연차는 15일입니다 [1]."), audit=audit, top_k=5)

    user = _user(groups=("n:1",))   # n:2 문서는 접근 불가 → 제외
    ans = pipe.answer("연차 며칠?", user, today=date(2026, 7, 16))

    used_ids = [c.chunk_id for c in ans.used_chunks]
    assert "ok::0" in used_ids
    assert "secret::0" not in used_ids          # 권한 초과 문서 배제
    assert ans.citations and ans.citations[0].title == "연차규정"
    assert audit.events and audit.events[0]["action"] == "query"
    assert "secret::0" not in audit.events[0]["cited_chunk_ids"]


# ── 중국어권 모델이 흘리는 한자 차단 ────────────────────────────────────────
def test_hanja_leakage_is_detected_and_stripped():
    from app.search.answer import has_foreign_script, strip_foreign_leakage
    assert has_foreign_script("연차는 15일(年次)입니다")
    assert not has_foreign_script("연차는 15일입니다")
    # 근거에 없는 한자는 제거
    assert strip_foreign_leakage("연차는 15일(年次)입니다", "연차는 15일입니다") == \
        "연차는 15일입니다"
    assert strip_foreign_leakage("员工 규정입니다", "규정입니다") == "규정입니다"


def test_hanja_in_source_document_is_kept():
    """사규 원문에 한자가 있으면 정당한 인용이므로 남긴다."""
    from app.search.answer import strip_foreign_leakage
    src = "제1조(目的) 이 규정은 …"
    assert "目的" in strip_foreign_leakage("제1조(目的)에 따르면", src)


def test_generate_answer_retries_when_model_emits_chinese():
    from app.search.answer import generate_answer
    from app.search.types import RetrievedChunk

    chunks = [RetrievedChunk(chunk_id="c1", doc_id="d1", text="연차는 15일입니다.",
                             score=1.0, payload={}, title="규정")]
    calls = []

    class _Leaky:
        def complete_text(self, prompt):
            calls.append(prompt)
            # 첫 응답엔 한자를 섞고, 재요청에는 한국어로만 답한다
            return "年次는 15일입니다 [1]." if len(calls) == 1 else "연차는 15일입니다 [1]."

    ans = generate_answer(_Leaky(), "연차?", chunks)
    assert len(calls) == 2, "한자가 섞였는데 재요청하지 않음"
    assert ans.text == "연차는 15일입니다 [1]."


def test_answer_prompt_forbids_chinese():
    from app.search.answer import build_answer_prompt
    from app.search.types import RetrievedChunk
    prompt = build_answer_prompt("질문", [RetrievedChunk(
        chunk_id="c", doc_id="d", text="본문", score=1.0, payload={})])
    assert "한국어로만" in prompt and "한자" in prompt
