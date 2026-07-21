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
