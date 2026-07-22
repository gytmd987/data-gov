"""검색·답변 파이프라인 테스트 (외부 서비스 없이 fake 주입)."""

from datetime import date

from app.schemas.enums import SensitivityLevel
from app.search.access import AccessPolicy, UserContext
from app.search.answer import build_answer_prompt, generate_answer, parse_citations
from app.search.fusion import reciprocal_rank_fusion
from app.search.pipeline import SearchPipeline
from app.search.rerank import rerank_chunks
from app.search.retriever import HybridRetriever
from app.search.types import RetrievedChunk


def _chunk(cid, text="본문", *, groups=("hr_core",), rank=1, status="active",
           expiry=None, superseded=None, title="문서", page=1, score=0.0):
    return RetrievedChunk(
        chunk_id=cid, text=text, score=score,
        payload={
            "chunk_id": cid, "parent_doc_id": cid.split("::")[0],
            "access_groups": list(groups), "sensitivity_rank": rank,
            "status": status, "expiry_date": expiry, "superseded_by": superseded,
            "title": title, "page_no": page,
        })


def _user(groups=("hr_core",), clearance=SensitivityLevel.CONFIDENTIAL):
    return UserContext(user_id="u1", groups=frozenset(groups), clearance=clearance)


# ── 접근통제 allows() ────────────────────────────────────────────────────────
def test_allows_permits_matching():
    pol = AccessPolicy.for_user(_user(), today=date(2026, 7, 16))
    assert pol.allows(_chunk("d::0").payload)


def test_allows_denies_group_mismatch():
    pol = AccessPolicy.for_user(_user(groups=("payroll",)), today=date(2026, 7, 16))
    assert not pol.allows(_chunk("d::0", groups=("hr_core",)).payload)


def test_allows_denies_over_clearance():
    pol = AccessPolicy.for_user(_user(clearance=SensitivityLevel.INTERNAL),
                                today=date(2026, 7, 16))
    # restricted(rank 3) > internal(rank 1)
    assert not pol.allows(_chunk("d::0", rank=3).payload)


def test_allows_denies_non_active_expired_superseded():
    pol = AccessPolicy.for_user(_user(), today=date(2026, 7, 16))
    assert not pol.allows(_chunk("d::0", status="archived").payload)
    assert not pol.allows(_chunk("d::0", expiry="2020-01-01").payload)
    assert not pol.allows(_chunk("d::0", superseded="newdoc").payload)


def test_allows_wildcard_open_to_all_groups():
    # "*" = 전체 공개 — 그룹이 전혀 안 겹치는 사용자도 통과
    pol = AccessPolicy.for_user(_user(groups=("payroll",)), today=date(2026, 7, 16))
    assert pol.allows(_chunk("d::0", groups=("*",)).payload)


def test_allows_wildcard_still_checks_clearance():
    # 전체 공개여도 민감도 등급은 그대로 적용
    pol = AccessPolicy.for_user(_user(clearance=SensitivityLevel.INTERNAL),
                                today=date(2026, 7, 16))
    assert not pol.allows(_chunk("d::0", groups=("*",), rank=3).payload)


def test_to_qdrant_filter_builds():
    pol = AccessPolicy.for_user(_user())
    f = pol.to_qdrant_filter()
    # must 조건 3개(groups/sensitivity/status)
    assert len(f.must) == 3
    # 그룹 필터에 전체 공개 센티널 "*" 포함(전체 공개 문서도 후보에 들어오도록)
    assert "*" in f.must[0].match.any


# ── 과거 문서 포함(include_past) ──────────────────────────────────────────────
def test_include_past_permits_expired_superseded_archived():
    pol = AccessPolicy.for_user(_user(), today=date(2026, 7, 16), include_past=True)
    assert pol.allows(_chunk("d::0", status="archived").payload)
    assert pol.allows(_chunk("d::0", expiry="2020-01-01").payload)
    assert pol.allows(_chunk("d::0", superseded="newdoc").payload)


def test_include_past_still_enforces_group_and_clearance():
    # 과거 포함이어도 그룹 불일치는 거부
    pol = AccessPolicy.for_user(_user(groups=("payroll",)),
                                today=date(2026, 7, 16), include_past=True)
    assert not pol.allows(_chunk("d::0", groups=("hr_core",), status="expired").payload)
    # 민감도 초과도 거부
    pol2 = AccessPolicy.for_user(_user(clearance=SensitivityLevel.INTERNAL),
                                 today=date(2026, 7, 16), include_past=True)
    assert not pol2.allows(_chunk("d::0", rank=3, status="expired").payload)


def test_to_qdrant_filter_include_past_drops_status():
    pol = AccessPolicy.for_user(_user(), include_past=True)
    f = pol.to_qdrant_filter()
    # status==active 조건이 빠져 groups/sensitivity 2개만 남음
    assert len(f.must) == 2


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
    # 허용 청크 + 권한초과(rank 3) 청크를 dense가 함께 반환 → 후처리에서 배제되어야
    allowed = _chunk("ok::0", "연차는 15일 [approved]", rank=1, title="연차규정")
    restricted = _chunk("secret::0", "급여 정보", rank=3, title="급여표")
    retriever = HybridRetriever(dense=FakeDense([allowed, restricted]))
    reranker = FakeReranker([0.9, 0.8])
    audit = FakeAudit()
    pipe = SearchPipeline(
        retriever=retriever, reranker=reranker,
        llm=FakeLLM("연차는 15일입니다 [1]."), audit=audit, top_k=5)

    user = _user(clearance=SensitivityLevel.INTERNAL)   # rank 1 → restricted 제외
    ans = pipe.answer("연차 며칠?", user, today=date(2026, 7, 16))

    used_ids = [c.chunk_id for c in ans.used_chunks]
    assert "ok::0" in used_ids
    assert "secret::0" not in used_ids          # 권한 초과 문서 배제
    assert ans.citations and ans.citations[0].title == "연차규정"
    assert audit.events and audit.events[0]["action"] == "query"
    assert "secret::0" not in audit.events[0]["cited_chunk_ids"]
