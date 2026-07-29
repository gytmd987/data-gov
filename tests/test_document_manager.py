"""문서 관리 테스트 — 수정·수동 버전연결·보관·삭제가 검색에 반영되는지(실 Qdrant in-memory)."""

from datetime import date
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from app.clients.qdrant_indexer import QdrantIndexer
from app.demo.offline import (
    EMBED_DIM,
    build_offline_review_service,
    build_offline_search_pipeline,
    make_offline_engine,
)
from app.db.repositories import OrgRepository, UserRepository
from app.manage.service import DocumentManager
from app.schemas.metadata import GovernanceBlock


@pytest.fixture
def env(tmp_path: Path):
    session = Session(make_offline_engine(), expire_on_commit=False)
    qdrant = QdrantClient(location=":memory:")
    # 조직도 + 사용자(파트B 파트원)
    org = OrgRepository(session)
    team = org.create_node("People팀", "team")
    org.create_node("파트B", "part", parent_id=team.id)
    partB = org.list_nodes()[-1]
    UserRepository(session).set_org("u", partB.id, "파트원")
    session.commit()
    svc = build_offline_review_service(session, qdrant)

    def ingest(name, text, selections=None):
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        doc_id = svc.start_ingestion(str(p), ingested_by="t")
        gov = GovernanceBlock(access_selections=selections or [])  # 빈=팀 전체
        svc.submit_review(doc_id, governance=gov, lifecycle_overrides={"status": "active"})
        return doc_id

    a = ingest("leave_2025.txt", "연차 규정 2025. 연차는 15일이다.")
    b = ingest("leave_2026.txt", "연차 규정 2026 개정. 연차는 20일이다.")
    pipe = build_offline_search_pipeline(session, qdrant)
    manager = DocumentManager(session, indexer=QdrantIndexer(
        collection="hr_chunks", vector_size=EMBED_DIM, client=qdrant))
    user = UserRepository(session).get_user_context("u")   # n:{partB}
    return session, pipe, manager, user, a, b


def _files(pipe, user, q="연차"):
    ans = pipe.answer(q, user, today=date(2026, 7, 20))
    return {c.source_filename for c in ans.used_chunks}


def test_qa_chunk_surfaces_doc_for_question(env):
    # 오프라인 LLM이 생성한 예상 질문으로 검색 → 합성 Q&A 청크가 문서를 노출시킴
    session, pipe, manager, user, a, b = env
    ans = pipe.answer("이 문서는 무엇에 대한 것인가요?", user, today=date(2026, 7, 20))
    assert {c.source_filename for c in ans.used_chunks}   # 최소 한 문서 매칭


def test_supersede_excludes_old_from_search(env):
    session, pipe, manager, user, a, b = env
    assert "leave_2025.txt" in _files(pipe, user)      # 초기엔 구버전도 검색됨

    manager.supersede(a, b)                             # 2025 → 2026의 이전 버전

    files = _files(pipe, user)
    assert "leave_2025.txt" not in files               # 구버전 검색 제외 ✅
    assert "leave_2026.txt" in files


def test_update_metadata_syncs_access(env):
    session, pipe, manager, user, a, b = env
    # 접근을 사용자가 속하지 않은 노드로 제한하면 더 이상 못 봄
    secret = OrgRepository(session).create_node("비밀파트", "part")
    session.commit()
    manager.update_metadata(a, governance=GovernanceBlock(
        access_selections=[f"node:{secret.id}"]))
    files = _files(pipe, user)
    assert "leave_2025.txt" not in files               # 권한 변경이 검색에 반영 ✅


def test_archived_searchable_but_expired_excluded(env):
    from app.schemas.enums import DocStatus
    session, pipe, manager, user, a, b = env
    # 보관은 기본 검색에 노출(대부분 문서가 보관)
    manager.archive(a)
    assert "leave_2025.txt" in _files(pipe, user)
    # 만료는 기본 검색에서 제외
    manager.set_status(a, DocStatus.EXPIRED)
    assert "leave_2025.txt" not in _files(pipe, user)


def test_list_and_hard_delete(env):
    session, pipe, manager, user, a, b = env
    docs = manager.list_documents()
    assert {d["filename"] for d in docs} == {"leave_2025.txt", "leave_2026.txt"}

    manager.delete(a, hard=True)
    assert "leave_2025.txt" not in {d["filename"] for d in manager.list_documents()}
    assert "leave_2025.txt" not in _files(pipe, user)


# ── 대량 대비: 페이징·필터·카운트 ────────────────────────────────────────────
def test_pagination_and_status_filter(env):
    session, pipe, manager, user, a, b = env
    assert manager.count_documents() == 2

    page1 = manager.list_documents(limit=1, offset=0)
    page2 = manager.list_documents(limit=1, offset=1)
    assert len(page1) == 1 and len(page2) == 1
    assert page1[0]["doc_id"] != page2[0]["doc_id"]      # 페이지가 겹치지 않음

    assert manager.count_documents(lifecycle_status="active") == 2
    assert manager.count_documents(lifecycle_status="archived") == 0
    manager.archive(a)
    assert manager.count_documents(lifecycle_status="archived") == 1
    assert manager.count_documents(lifecycle_status="active") == 1


# ── 생애주기 자동화(만료 sweep) ──────────────────────────────────────────────
def test_lifecycle_sweep_materializes_expired(env):
    from app.manage.lifecycle import sweep_expired
    session, pipe, manager, user, a, b = env
    # a에 과거 만료일 지정 — 상태는 여전히 active(자동 전환 전)
    manager.update_metadata(a, lifecycle_overrides={"expiry_date": "2020-01-01"})
    assert manager.count_documents(lifecycle_status="expired") == 0

    swept = sweep_expired(manager, today=date(2026, 7, 20))
    assert a in swept
    assert manager.count_documents(lifecycle_status="expired") == 1
    assert manager.count_documents(lifecycle_status="active") == 1


# ── 과거 문서 포함 검색 ──────────────────────────────────────────────────────
def test_include_past_surfaces_expired_doc(env):
    session, pipe, manager, user, a, b = env
    manager.update_metadata(a, lifecycle_overrides={"status": "expired"})

    # 기본 검색: 만료 문서 제외
    default = {c.source_filename for c in
               pipe.answer("연차", user, today=date(2026, 7, 20)).used_chunks}
    assert "leave_2025.txt" not in default

    # 과거 포함: 만료 문서도 검색됨
    past = pipe.answer("연차", user, today=date(2026, 7, 20), include_past=True)
    assert "leave_2025.txt" in {c.source_filename for c in past.used_chunks}
