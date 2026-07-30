"""문서 가시성(권한) 회귀 테스트.

여기가 깨지면 **권한 없는 문서가 목록·자동완성에 새어 나간다**. 보안 회귀이므로
UI 가 아니라 쿼리 계층에서 검증한다.

조직도: People팀 > ㅁ그룹 > (ㄴ파트, ㅇ파트), People팀 > ㅅ그룹
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.repositories import DocumentRepository, OrgRepository, UserRepository
from app.schemas.enums import DocStatus, DocType, FileFormat
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import (
    ClassificationBlock,
    DocumentMetadata,
    GovernanceBlock,
    IdentificationBlock,
    LifecycleBlock,
)
from app.search.access import Visibility


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def org(session):
    repo = OrgRepository(session)
    team = repo.create_node("People팀", "team")
    m = repo.create_node("ㅁ그룹", "group", parent_id=team.id)
    n = repo.create_node("ㄴ파트", "part", parent_id=m.id)
    o = repo.create_node("ㅇ파트", "part", parent_id=m.id)
    s_grp = repo.create_node("ㅅ그룹", "group", parent_id=team.id)
    session.commit()
    return {"team": team.id, "ㅁ": m.id, "ㄴ": n.id, "ㅇ": o.id, "ㅅ": s_grp.id}


def _make_doc(session, doc_id: str, *, author_node: int, selections: list[str],
              title: str = "문서") -> str:
    """검토 확정과 같은 경로(열람 토큰 확장 + upsert)로 색인 완료 문서를 만든다."""
    tree = OrgRepository(session).load_tree()
    doc = DocumentMetadata(
        identification=IdentificationBlock(
            doc_id=doc_id, source_filename=f"{title}.txt",
            file_format=FileFormat.TXT, file_hash=doc_id * 4,
            ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ingested_by="uploader@co.com"),
        classification=ClassificationBlock(doc_type=DocType("report"),
                                           title_normalized=title),
        governance=GovernanceBlock(author_node_id=author_node,
                                   access_selections=selections,
                                   access_tokens=tree.readable_tokens(selections)),
        lifecycle=LifecycleBlock(status=DocStatus.ARCHIVED))
    DocumentRepository(session).upsert_document(doc, IngestionStatus.INDEXED)
    session.commit()
    return doc_id


def _member(session, uid: str, node_id: int) -> Visibility:
    users = UserRepository(session)
    users.set_memberships(uid, [node_id], display_name=uid)
    session.commit()
    ctx = users.get_user_context(uid)
    return Visibility(read_tokens=frozenset(ctx.groups))


def _titles(session, vis) -> set[str]:
    return {d["title"] for d in
            DocumentRepository(session).list_documents(visible_to=vis)}


# ── 이슈: 상위 부서 공개 문서가 형제 파트에 안 보이던 문제 ────────────────────
def test_group_wide_doc_is_visible_to_sibling_part(session, org):
    """ㄴ파트 폴더에 'ㅁ그룹 공개'로 올린 문서는 ㅇ파트 사람에게도 보여야 한다."""
    _make_doc(session, "d1", author_node=org["ㄴ"], selections=[f"node:{org['ㅁ']}"],
              title="그룹공개문서")
    vis = _member(session, "o@co.com", org["ㅇ"])
    assert _titles(session, vis) == {"그룹공개문서"}


def test_part_only_doc_is_hidden_from_sibling_part(session, org):
    """반대로 'ㄴ파트 공개'면 ㅇ파트 사람에겐 보이면 안 된다."""
    _make_doc(session, "d2", author_node=org["ㄴ"], selections=[f"node:{org['ㄴ']}"],
              title="파트전용문서")
    vis = _member(session, "o@co.com", org["ㅇ"])
    assert _titles(session, vis) == set()


def test_other_group_doc_is_hidden(session, org):
    _make_doc(session, "d3", author_node=org["ㅅ"], selections=[f"node:{org['ㅅ']}"],
              title="타그룹문서")
    vis = _member(session, "o@co.com", org["ㅇ"])
    assert _titles(session, vis) == set()


def test_public_doc_is_visible_to_everyone(session, org):
    """열람 대상을 안 고르면 팀 전체 공개(`*`)."""
    _make_doc(session, "d4", author_node=org["ㅅ"], selections=[], title="전체공개")
    vis = _member(session, "o@co.com", org["ㅇ"])
    assert _titles(session, vis) == {"전체공개"}


# ── 자동완성(연관/버전 지정)이 권한을 우회하지 못한다 ────────────────────────
def test_search_autocomplete_respects_permissions(session, org):
    _make_doc(session, "d5", author_node=org["ㅅ"], selections=[f"node:{org['ㅅ']}"],
              title="비밀 계약서")
    vis = _member(session, "o@co.com", org["ㅇ"])
    rows = DocumentRepository(session).list_documents(
        text="비밀", indexed_only=True, limit=20, visible_to=vis)
    assert rows == []


def test_count_matches_list_under_permissions(session, org):
    """건수·페이징도 같은 필터를 타야 한다(개수만 새어 나가는 것도 유출)."""
    _make_doc(session, "d6", author_node=org["ㅅ"], selections=[f"node:{org['ㅅ']}"])
    _make_doc(session, "d7", author_node=org["ㅇ"], selections=[f"node:{org['ㅇ']}"])
    vis = _member(session, "o@co.com", org["ㅇ"])
    repo = DocumentRepository(session)
    assert repo.count_documents(visible_to=vis) == 1
    assert len(repo.list_documents(visible_to=vis)) == 1


def test_readable_doc_ids_filters_related_links(session, org):
    """연관 문서 표시도 권한을 통과한 id 만 남는다."""
    _make_doc(session, "d8", author_node=org["ㅇ"], selections=[f"node:{org['ㅇ']}"])
    _make_doc(session, "d9", author_node=org["ㅅ"], selections=[f"node:{org['ㅅ']}"])
    vis = _member(session, "o@co.com", org["ㅇ"])
    assert DocumentRepository(session).readable_doc_ids(["d8", "d9"], vis) == {"d8"}


# ── 관리 권한은 열람 토큰과 별개로 유지된다 ─────────────────────────────────
def test_dept_head_sees_own_dept_docs_even_without_token(session, org):
    """부서장은 열람 토큰이 없어도 자기 부서 문서를 관리해야 하므로 보인다."""
    _make_doc(session, "d10", author_node=org["ㄴ"], selections=[f"node:{org['ㅅ']}"],
              title="권한이 좁혀진 문서")
    vis = Visibility(read_tokens=frozenset({f"n:{org['ㅁ']}"}),
                     manage_node_ids=frozenset({org["ㅁ"], org["ㄴ"], org["ㅇ"]}))
    assert _titles(session, vis) == {"권한이 좁혀진 문서"}


def test_admin_sees_everything(session, org):
    _make_doc(session, "d11", author_node=org["ㅅ"], selections=[f"node:{org['ㅅ']}"])
    assert len(DocumentRepository(session).list_documents(
        visible_to=Visibility.admin())) == 1


# ── 권한 변경이 즉시 반영된다(토큰 테이블 동기화) ───────────────────────────
def test_access_change_takes_effect_immediately(session, org):
    doc_id = _make_doc(session, "d12", author_node=org["ㄴ"],
                       selections=[f"node:{org['ㄴ']}"], title="권한바뀔문서")
    vis = _member(session, "o@co.com", org["ㅇ"])
    assert _titles(session, vis) == set()

    repo = DocumentRepository(session)
    doc = repo.get(doc_id)
    tree = OrgRepository(session).load_tree()
    sels = [f"node:{org['ㅁ']}"]
    doc.governance = doc.governance.model_copy(update={
        "access_selections": sels, "access_tokens": tree.readable_tokens(sels)})
    repo.upsert_document(doc, IngestionStatus.INDEXED)
    session.commit()
    assert _titles(session, vis) == {"권한바뀔문서"}
