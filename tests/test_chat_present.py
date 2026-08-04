"""ChatRepository + group_sources 테스트."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.repositories import ChatRepository
from app.search.present import group_sources
from app.search.types import Answer, Citation, RetrievedChunk


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


# ── ChatRepository ───────────────────────────────────────────────────────────
def test_conversation_flow(session):
    chat = ChatRepository(session)
    cid = chat.create_conversation("hong")
    chat.add_message(cid, "user", "연차 며칠?", use_rag=True)
    chat.add_message(cid, "assistant", "15일입니다 [1].", use_rag=True,
                     sources=[{"label": "연차규정"}])
    session.commit()

    convs = chat.list_conversations("hong")
    assert len(convs) == 1
    assert convs[0]["title"] == "연차 며칠?"          # 첫 질문이 제목

    msgs = chat.get_messages(cid, user_id="hong")
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["sources"][0]["label"] == "연차규정"


def test_other_users_conversation_blocked(session):
    chat = ChatRepository(session)
    cid = chat.create_conversation("hong")
    chat.add_message(cid, "user", "비밀 질문")
    session.commit()
    assert chat.get_messages(cid, user_id="kim") == []   # 남의 대화 차단


def test_get_messages_limit(session):
    chat = ChatRepository(session)
    cid = chat.create_conversation("hong")
    for i in range(15):
        chat.add_message(cid, "user", f"m{i}")
    msgs = chat.get_messages(cid, limit=10)
    assert len(msgs) == 10 and msgs[-1]["text"] == "m14"


# ── group_sources ────────────────────────────────────────────────────────────
def _chunk(cid, fn, text):
    return RetrievedChunk(chunk_id=cid, text=text, score=1.0,
                          payload={"parent_doc_id": fn, "source_filename": fn,
                                   "title": None, "page_no": 1})


def test_group_sources_merges_same_file():
    chunks = [_chunk("a::0", "a.txt", "구절1"), _chunk("a::1", "a.txt", "구절2"),
              _chunk("b::0", "b.txt", "구절3")]
    ans = Answer(text="답 [1][2][3]", used_chunks=chunks, citations=[
        Citation(marker=1, doc_id="a.txt", source_filename="a.txt", chunk_id="a::0", page_no=1),
        Citation(marker=2, doc_id="a.txt", source_filename="a.txt", chunk_id="a::1", page_no=2),
        Citation(marker=3, doc_id="b.txt", source_filename="b.txt", chunk_id="b::0", page_no=1),
    ])
    groups = group_sources(ans)
    assert len(groups) == 2                              # 파일 2개 → 출처 2개
    a = next(g for g in groups if g["doc_id"] == "a.txt")
    assert a["markers"] == [1, 2]                        # 마커 병합
    assert [p["text"] for p in a["passages"]] == ["구절1", "구절2"]
    assert a["is_past"] is False                         # 현행 문서 → 과거 아님


def test_group_sources_flags_past_documents():
    from datetime import date

    def past_chunk(cid, fn, **payload):
        return RetrievedChunk(chunk_id=cid, text="본문", score=1.0,
                              payload={"parent_doc_id": fn, "source_filename": fn,
                                       "title": None, "page_no": 1, **payload})

    chunks = [past_chunk("exp::0", "expired.txt", status="expired"),
              past_chunk("old::0", "old.txt", superseded_by="new"),
              past_chunk("cur::0", "current.txt", status="active")]
    ans = Answer(text="답 [1][2][3]", used_chunks=chunks, citations=[
        Citation(marker=1, doc_id="expired.txt", chunk_id="exp::0"),
        Citation(marker=2, doc_id="old.txt", chunk_id="old::0"),
        Citation(marker=3, doc_id="current.txt", chunk_id="cur::0"),
    ])
    groups = {g["doc_id"]: g for g in group_sources(ans, today=date(2026, 7, 20))}
    assert groups["expired.txt"]["is_past"] is True      # 만료 상태
    assert groups["old.txt"]["is_past"] is True          # 대체됨
    assert groups["current.txt"]["is_past"] is False     # 현행


# ── 인용 번호를 화면의 출처 번호와 맞춘다 ───────────────────────────────────
def test_renumber_citations_matches_source_order():
    """본문 [청크번호] → 화면 [출처번호]. 어긋나면 읽는 사람이 헷갈린다."""
    from app.search.present import renumber_citations
    sources = [{"index": 1, "markers": [2, 3]}, {"index": 2, "markers": [5]}]
    # 인용 앞 공백은 붙여 준다(화면에서 번호 칩이 문장에 딱 붙어야 읽기 좋다)
    assert renumber_citations("연차는 15일 [2]. 신청은 인사팀 [5].", sources) == \
        "연차는 15일[1]. 신청은 인사팀[2]."


def test_renumber_merges_same_document_markers():
    """한 문서의 여러 청크를 인용하면 하나의 출처 번호로 합친다."""
    from app.search.present import renumber_citations
    sources = [{"index": 1, "markers": [1, 2]}]
    assert renumber_citations("가 [1] 나 [2]", sources) == "가[1] 나[1]"
    assert renumber_citations("가 [1][2]", sources) == "가[1]"


def test_renumber_drops_unmatched_markers():
    """출처에 없는 번호(모델이 지어낸 것)는 지운다."""
    from app.search.present import renumber_citations
    sources = [{"index": 1, "markers": [1]}]
    assert renumber_citations("연차는 15일입니다 [7].", sources) == "연차는 15일입니다."


def test_group_sources_assigns_display_index():
    from app.search.present import group_sources
    from app.search.types import Answer, Citation, RetrievedChunk
    chunks = [RetrievedChunk(chunk_id="c1", doc_id="d1", text="본문", score=1.0,
                             payload={"status": "active"}, title="규정"),
              RetrievedChunk(chunk_id="c2", doc_id="d2", text="본문2", score=0.9,
                             payload={"status": "active"}, title="지침")]
    ans = Answer(text="x [1] y [2]", used_chunks=chunks, citations=[
        Citation(marker=1, doc_id="d1", title="규정", chunk_id="c1"),
        Citation(marker=2, doc_id="d2", title="지침", chunk_id="c2")])
    assert [g["index"] for g in group_sources(ans)] == [1, 2]
