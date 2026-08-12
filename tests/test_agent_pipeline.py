"""판단 루프 + 스트리밍을 실제 저장소·검색기와 함께(오프라인).

`test_agent.py` 는 루프 자체를 대역으로 검증한다. 여기서는 진짜 문서·권한·Qdrant 를
붙여 **도구가 실제로 남의 문서를 못 보는지**와 **이벤트가 제대로 나오는지**를 본다.
"""

from datetime import date
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from app.demo.offline import (
    build_offline_review_service,
    build_offline_search_pipeline,
    make_offline_engine,
)
from app.db.repositories import OrgRepository, UserRepository
from app.schemas.metadata import GovernanceBlock
from app.search.access import AccessPolicy, Visibility
from app.search.tools import ToolBox

TODAY = date(2026, 7, 20)


@pytest.fixture
def env(tmp_path: Path):
    session = Session(make_offline_engine(), expire_on_commit=False)
    qdrant = QdrantClient(location=":memory:")
    org = OrgRepository(session)
    team = org.create_node("People팀", "team")
    mine = org.create_node("우리파트", "part", parent_id=team.id)
    other = org.create_node("남의파트", "part", parent_id=team.id)
    users = UserRepository(session)
    users.set_org("me", mine.id, "파트원")
    session.commit()

    svc = build_offline_review_service(session, qdrant)

    def ingest(name, text, node, selections):
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        doc_id = svc.start_ingestion(str(p), ingested_by="t", folder_node_id=node.id)
        svc.submit_review(doc_id, governance=GovernanceBlock(
            author_node_id=node.id, access_selections=selections),
            lifecycle_overrides={"status": "active"})
        return doc_id

    ours = ingest("연차규정.txt", "연차는 15일이며 인사팀에 신청한다.",
                  mine, [f"node:{mine.id}"])
    theirs = ingest("남의파트 대외비.txt", "남의파트 인사 검토 메모. 연차 관련 대외비.",
                    other, [f"node:{other.id}"])
    session.commit()

    pipe = build_offline_search_pipeline(session, qdrant)
    user = users.get_user_context("me")
    return session, pipe, user, ours, theirs


def _box(session, pipe, user):
    policy = AccessPolicy.for_user(user, today=TODAY)
    return ToolBox(session=session, policy=policy,
                   visibility=Visibility(read_tokens=frozenset(user.groups)),
                   retrieve=pipe._retrieve_ranked, today=TODAY)


# ── 도구별 권한 ──────────────────────────────────────────────────────────────
def test_read_document_refuses_a_document_outside_my_permission(env):
    session, pipe, user, ours, theirs = env
    box = _box(session, pipe, user)

    ok = box.read_document(문서id=ours)
    blocked = box.read_document(문서id=theirs)

    assert "연차는 15일" in ok.text
    assert "대외비" not in blocked.text, "권한 밖 문서 본문이 새어 나갔다"
    assert blocked.text == "그런 문서가 없습니다."   # 존재 여부도 알려주지 않는다


def test_list_documents_only_shows_what_i_can_read(env):
    session, pipe, user, ours, theirs = env
    box = _box(session, pipe, user)

    res = box.list_documents(개수=50)

    assert ours in res.text
    assert theirs not in res.text, "권한 밖 문서가 목록에 나왔다"


def test_count_documents_does_not_leak_the_number_of_hidden_documents(env):
    session, pipe, user, ours, theirs = env
    box = _box(session, pipe, user)

    assert box.count_documents().text == "조건에 맞는 문서: 1건"


def test_search_tool_excludes_other_departments(env):
    session, pipe, user, ours, theirs = env
    box = _box(session, pipe, user)

    res = box.search_chunks(검색어="연차")

    assert res.chunks, "볼 수 있는 문서도 안 나왔다"
    assert all(c.doc_id == ours for c in res.chunks)


def test_superseded_documents_drop_out_of_list_and_count(env):
    """대체된 문서는 채팅 검색에서 빠진다 — 목록·세기도 같아야 답이 어긋나지 않는다."""
    session, pipe, user, ours, theirs = env
    from app.manage.service import DocumentManager

    box = _box(session, pipe, user)
    assert box.count_documents().text == "조건에 맞는 문서: 1건"

    # 우리 문서를 남의 문서로 대체 처리 → 더 이상 현행이 아니다
    DocumentManager(session).supersede(ours, theirs)

    box = _box(session, pipe, user)
    assert box.count_documents().text == "조건에 맞는 문서: 0건"
    assert box.list_documents().text == "조건에 맞는 문서가 없습니다."
    assert box.read_document(문서id=ours).text == "그런 문서가 없습니다."


def test_list_documents_narrows_by_doc_type(env):
    session, pipe, user, ours, theirs = env
    box = _box(session, pipe, user)

    # 오프라인 분류기는 report 로 넣는다 → 없는 종류로 좁히면 0건이어야 한다
    assert box.list_documents(문서종류=["contract"]).text == "조건에 맞는 문서가 없습니다."


# ── 파이프라인 이벤트 ────────────────────────────────────────────────────────
def _events(pipe, user, session, **kw):
    return list(pipe.answer_events("연차는 며칠인가요?", user, session=session,
                                   today=TODAY, **kw))


def test_answer_events_stream_deltas_then_a_final_answer(env):
    session, pipe, user, ours, theirs = env

    events = _events(pipe, user, session)

    kinds = [e["type"] for e in events]
    assert "delta" in kinds, "답변이 조각으로 흘러나오지 않았다"
    assert kinds[-1] == "answer", "마지막은 최종 답변이어야 한다"
    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    assert streamed, "흘려보낸 내용이 비어 있다"


def test_final_answer_matches_what_was_streamed(env):
    """화면은 흘러온 글자를 최종본으로 갈아 끼운다 — 둘이 크게 어긋나면 깜빡인다."""
    session, pipe, user, ours, theirs = env

    events = _events(pipe, user, session)

    streamed = "".join(e["text"] for e in events if e["type"] == "delta")
    final = events[-1]["answer"].text
    assert final.strip() in streamed or streamed.strip().startswith(final[:20])


def test_planning_off_still_answers(env):
    session, pipe, user, ours, theirs = env

    events = _events(pipe, user, session, plan=False)

    assert events[-1]["type"] == "answer"
    assert not [e for e in events if e["type"] == "step"]


def test_answer_events_never_cite_a_document_i_cannot_read(env):
    session, pipe, user, ours, theirs = env

    events = list(pipe.answer_events("대외비 메모 내용 알려줘", user, session=session,
                                     today=TODAY))

    answer = events[-1]["answer"]
    assert all(c.doc_id != theirs for c in answer.used_chunks)
    assert all(c.doc_id != theirs for c in answer.citations)


def test_a_broken_planner_still_produces_an_answer(env):
    """판단이 죽어도 기존 경로(검색 1회)로 답이 나와야 한다."""
    session, pipe, user, ours, theirs = env

    class Broken:
        def complete_json(self, prompt, schema):
            raise RuntimeError("모델 죽음")

        def complete_text(self, prompt, temperature=0.2):
            return pipe.llm.complete_text(prompt)

    broken = pipe.__class__(retriever=pipe.retriever, reranker=pipe.reranker,
                            llm=Broken(), audit=pipe.audit)
    events = list(broken.answer_events("연차는 며칠인가요?", user, session=session,
                                       today=TODAY))

    assert events[-1]["type"] == "answer"
    assert events[-1]["answer"].text
