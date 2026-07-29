"""조직도 트리 헬퍼 + OrgRepository 테스트 (Phase 1)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.repositories import OrgRepository, UserRepository
from app.org.tree import OrgNodeView, OrgTree, user_tokens


# People팀 > (그룹A > 파트A1, 파트A2), (그룹B > 파트B1)
def _tree():
    nodes = [
        OrgNodeView(1, "People팀", "team", None),
        OrgNodeView(2, "그룹A", "group", 1),
        OrgNodeView(3, "파트A1", "part", 2),
        OrgNodeView(4, "파트A2", "part", 2),
        OrgNodeView(5, "그룹B", "group", 1),
        OrgNodeView(6, "파트B1", "part", 5),
    ]
    return OrgTree(nodes)


def test_ancestors_and_subtree():
    t = _tree()
    assert t.ancestors(3) == [2, 1]          # 파트A1 → 그룹A → People팀
    assert t.ancestors(1) == []
    assert set(t.subtree(2)) == {2, 3, 4}    # 그룹A + 두 파트
    assert t.subtree(3) == [3]               # 리프


def test_user_tokens_head_vs_member():
    assert user_tokens(3, "파트원") == ["n:3"]
    assert set(user_tokens(3, "파트장")) == {"n:3", "h:3"}
    assert user_tokens(None, "파트장") == []


def test_readable_tokens_empty_is_public():
    assert _tree().readable_tokens([]) == ["*"]


def test_readable_tokens_node_includes_subtree_and_ancestor_heads():
    # 그룹A 전체 공개 → 그룹A 하위 전원(n:2,3,4) + 상위 팀장(h:1)
    toks = set(_tree().readable_tokens(["node:2"]))
    assert toks == {"n:2", "n:3", "n:4", "h:1"}


def test_readable_tokens_head_only():
    # 파트A1 부서장만 → h:3 + 상위 부서장(h:2, h:1)
    toks = set(_tree().readable_tokens(["head:3"]))
    assert toks == {"h:3", "h:2", "h:1"}


def test_mixed_selection_matches_expected_readers():
    # 파트A1은 파트장만, 파트B1은 전원
    toks = set(_tree().readable_tokens(["head:3", "node:6"]))
    # 파트A1 파트원(n:3)은 못 봄, 파트장(h:3)은 봄
    assert "h:3" in toks and "n:3" not in toks
    # 파트B1 전원(n:6)은 봄
    assert "n:6" in toks
    # 상위 부서장들
    assert {"h:2", "h:1", "h:5"} <= toks

    def can_read(user_toks):
        return bool(set(user_toks) & toks)

    assert not can_read(user_tokens(3, "파트원"))   # 파트A1 파트원 ✗
    assert can_read(user_tokens(3, "파트장"))        # 파트A1 파트장 ✓
    assert can_read(user_tokens(6, "파트원"))        # 파트B1 파트원 ✓
    assert can_read(user_tokens(5, "그룹장"))        # 그룹B 그룹장(상위) ✓
    assert can_read(user_tokens(1, "팀장"))          # 팀장(최상위) ✓


# ── OrgRepository (DB) ───────────────────────────────────────────────────────
@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


def test_node_selection_grants_subtree_and_upper_heads():
    """지금 유일한 선택지인 '부서(node:)' 의 열람 범위."""
    tree = OrgTree([OrgNodeView(1, "People팀", "team", None),
                    OrgNodeView(2, "A그룹", "group", 1),
                    OrgNodeView(3, "A파트", "part", 2),
                    OrgNodeView(4, "B그룹", "group", 1)])
    toks = set(tree.readable_tokens(["node:2"]))
    assert {"n:2", "n:3"} <= toks      # 그 부서 + 하위 전원
    assert "h:1" in toks               # 상위 부서장
    assert "n:1" not in toks           # 상위 부서의 '일반 구성원'은 제외
    assert "n:4" not in toks           # 형제 부서 제외


def test_legacy_head_selection_still_readable():
    """UI에서 없앤 'head:'(부서장만)로 저장된 옛 문서도 그대로 해석된다."""
    tree = OrgTree([OrgNodeView(1, "People팀", "team", None),
                    OrgNodeView(2, "A그룹", "group", 1)])
    assert set(tree.readable_tokens(["head:2"])) == {"h:2", "h:1"}


def test_org_crud_and_tree(session):
    org = OrgRepository(session)
    team = org.create_node("People팀", "team")
    grp = org.create_node("그룹A", "group", parent_id=team.id)
    part = org.create_node("파트A1", "part", parent_id=grp.id)
    session.commit()

    tree = org.load_tree()
    assert tree.ancestors(part.id) == [grp.id, team.id]

    org.rename_node(grp.id, "채용그룹")
    session.commit()
    assert org.get(grp.id).name == "채용그룹"

    # 삭제 시 하위 cascade + 배정 사용자 해제
    users = UserRepository(session)
    users.set_org("kim@co.com", part.id, "파트장", display_name="김파트장")
    session.commit()
    assert org.members(part.id)[0]["user_id"] == "kim@co.com"

    org.delete_node(grp.id)   # 그룹 삭제 → 하위 파트도 삭제
    session.commit()
    assert org.get(part.id) is None
    ctx_user = users.list_users()[0]
    assert ctx_user["node_ids"] == []            # 소속 해제됨
    assert ctx_user["led_ids"] == []             # 부서장 지정도 해제됨
