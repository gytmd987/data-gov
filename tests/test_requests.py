"""Phase 4: 중복 판정 + 요청/승인 + 부서장 권한 테스트."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Document
from app.db.repositories import (
    DocumentRepository,
    OrgRepository,
    RequestRepository,
    UserRepository,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


def _doc(session, doc_id, title, fmt="txt", status="active", node=None):
    session.add(Document(doc_id=doc_id, source_filename=f"{title}.{fmt}",
                         file_format=fmt, file_hash=doc_id, status="indexed",
                         title=title, lifecycle_status=status, author_node_id=node,
                         access_groups=["*"], metadata_json={}))
    session.flush()


# ── 중복(제목+형식) 판정 ─────────────────────────────────────────────────────
def test_find_active_by_title_format(session):
    _doc(session, "d1", "연차규정", "txt")
    repo = DocumentRepository(session)
    hit = repo.find_active_by_title_format("연차규정", "txt")
    assert hit and hit["doc_id"] == "d1"
    # 형식 다르면 별개
    assert repo.find_active_by_title_format("연차규정", "pdf") is None
    # 자기 자신은 제외
    assert repo.find_active_by_title_format("연차규정", "txt", exclude_doc_id="d1") is None
    # 아카이브 문서는 중복으로 안 침
    _doc(session, "d2", "보관문서", "txt", status="archived")
    assert repo.find_active_by_title_format("보관문서", "txt") is None


# ── 요청/승인 리포지토리 ─────────────────────────────────────────────────────
def test_request_lifecycle_and_delete_lock(session):
    rr = RequestRepository(session)
    rid = rr.create("d1", "delete", "hong@co.com", doc_title="연차규정",
                    author_node_id=3, target_admin_id="admin@co.com", note="중복")
    session.flush()
    assert [r.id for r in rr.list_pending()] == [rid]
    # 삭제 요청 대기 → 같은 문서 잠금 집합에 포함
    assert rr.pending_delete_doc_ids() == {"d1"}

    rr.resolve(rid, "approved", "admin@co.com")
    session.flush()
    assert rr.list_pending() == []
    assert rr.pending_delete_doc_ids() == set()


# ── 부서장 관리 범위 ─────────────────────────────────────────────────────────
class _FakeUser:
    is_authenticated = True

    def __init__(self, email, staff=False):
        self.email = email
        self.username = email
        self.is_staff = staff


class _FakeDoc:
    def __init__(self, node):
        from app.schemas.metadata import DocumentMetadata, IdentificationBlock, GovernanceBlock
        from datetime import datetime, timezone
        self.governance = GovernanceBlock(author_node_id=node)


def test_can_manage_doc_scope(session):
    from web.authz import can_manage_doc, manage_scope
    org = OrgRepository(session)
    team = org.create_node("People팀", "team")
    grp = org.create_node("채용그룹", "group", parent_id=team.id)
    part = org.create_node("인터뷰파트", "part", parent_id=grp.id)
    users = UserRepository(session)
    users.set_org("lead@co.com", grp.id, "그룹장")   # 그룹장 → 그룹 subtree 관리
    users.set_org("member@co.com", part.id, "파트원")
    session.commit()

    lead = _FakeUser("lead@co.com")
    member = _FakeUser("member@co.com")
    admin = _FakeUser("admin@co.com", staff=True)

    # 그룹장: 하위 파트 문서 관리 가능, 그룹 밖(팀 직속) 문서는 불가
    assert can_manage_doc(session, lead, _FakeDoc(part.id))
    assert can_manage_doc(session, lead, _FakeDoc(grp.id))
    assert not can_manage_doc(session, lead, _FakeDoc(team.id))
    # 파트원: 관리 불가(요청만)
    assert not can_manage_doc(session, member, _FakeDoc(part.id))
    # 관리자: 전체 관리
    assert can_manage_doc(session, admin, _FakeDoc(team.id))
    is_adm, scope = manage_scope(session, lead)
    assert not is_adm and scope == {grp.id, part.id}
