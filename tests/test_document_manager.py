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
from app.db.repositories import UserRepository
from app.manage.service import DocumentManager
from app.schemas.enums import SensitivityLevel
from app.schemas.metadata import GovernanceBlock
from app.search.access import UserContext


@pytest.fixture
def env(tmp_path: Path):
    session = Session(make_offline_engine(), expire_on_commit=False)
    qdrant = QdrantClient(location=":memory:")
    UserRepository(session).upsert_group("hr_core")
    session.commit()
    svc = build_offline_review_service(session, qdrant)

    def ingest(name, text):
        p = tmp_path / name
        p.write_text(text, encoding="utf-8")
        doc_id = svc.start_ingestion(str(p), ingested_by="t")
        gov = GovernanceBlock(sensitivity_level=SensitivityLevel.INTERNAL,
                              contains_pii=False, access_groups=["hr_core"], owner="mgr")
        svc.submit_review(doc_id, governance=gov, lifecycle_overrides={"status": "active"})
        return doc_id

    a = ingest("leave_2025.txt", "연차 규정 2025. 연차는 15일이다.")
    b = ingest("leave_2026.txt", "연차 규정 2026 개정. 연차는 20일이다.")
    pipe = build_offline_search_pipeline(session, qdrant)
    manager = DocumentManager(session, indexer=QdrantIndexer(
        collection="hr_chunks", vector_size=EMBED_DIM, client=qdrant))
    user = UserContext("u", frozenset(["hr_core"]), SensitivityLevel.INTERNAL)
    return session, pipe, manager, user, a, b


def _files(pipe, user, q="연차"):
    ans = pipe.answer(q, user, today=date(2026, 7, 20))
    return {c.source_filename for c in ans.used_chunks}


def test_supersede_excludes_old_from_search(env):
    session, pipe, manager, user, a, b = env
    assert "leave_2025.txt" in _files(pipe, user)      # 초기엔 구버전도 검색됨

    manager.supersede(a, b)                             # 2025 → 2026의 이전 버전

    files = _files(pipe, user)
    assert "leave_2025.txt" not in files               # 구버전 검색 제외 ✅
    assert "leave_2026.txt" in files


def test_update_metadata_syncs_access(env):
    session, pipe, manager, user, a, b = env
    # 접근그룹을 payroll로 바꾸면 hr_core 사용자는 더 이상 못 봄
    manager.update_metadata(a, governance=GovernanceBlock(
        sensitivity_level=SensitivityLevel.RESTRICTED, contains_pii=True,
        pii_types=[], access_groups=["payroll"], owner="mgr"))
    files = _files(pipe, user)
    assert "leave_2025.txt" not in files               # 권한 변경이 검색에 반영 ✅


def test_archive_excludes_from_search(env):
    session, pipe, manager, user, a, b = env
    manager.archive(a)
    assert "leave_2025.txt" not in _files(pipe, user)


def test_list_and_hard_delete(env):
    session, pipe, manager, user, a, b = env
    docs = manager.list_documents()
    assert {d["filename"] for d in docs} == {"leave_2025.txt", "leave_2026.txt"}

    manager.delete(a, hard=True)
    assert "leave_2025.txt" not in {d["filename"] for d in manager.list_documents()}
    assert "leave_2025.txt" not in _files(pipe, user)
