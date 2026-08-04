"""부서 아래 하위 폴더 — 저장·분류용이며 **권한 주체가 아니다**.

폴더가 권한 주체가 되면 아무도 그 토큰을 갖지 않아 문서가 사라지거나,
반대로 폴더를 통해 권한이 새는 구멍이 된다. 그 경계를 여기서 고정한다.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.repositories import DocumentRepository, OrgRepository, UserRepository
from app.demo.offline import ExtractiveLLM, HashingEmbedder
from app.manage.storage import fs_dir, sync_with_disk
from app.org.tree import FOLDER, OrgNodeView, OrgTree
from app.review.service import ReviewService
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
    """People팀 > ㅁ그룹 > ㄴ파트 > (대외공개, 파트내부) 폴더"""
    repo = OrgRepository(session)
    team = repo.create_node("People팀", "team")
    grp = repo.create_node("ㅁ그룹", "group", parent_id=team.id)
    part = repo.create_node("ㄴ파트", "part", parent_id=grp.id)
    other = repo.create_node("ㅇ파트", "part", parent_id=grp.id)
    pub = repo.create_node("대외공개", FOLDER, parent_id=part.id)
    priv = repo.create_node("파트내부", FOLDER, parent_id=part.id)
    session.commit()
    return {"team": team.id, "그룹": grp.id, "ㄴ파트": part.id, "ㅇ파트": other.id,
            "대외공개": pub.id, "파트내부": priv.id}


# ── 폴더는 권한 주체가 아니다 ────────────────────────────────────────────────
def test_folder_resolves_to_its_department(session, org):
    tree = OrgRepository(session).load_tree()
    assert tree.org_node(org["대외공개"]) == org["ㄴ파트"]
    assert tree.org_node(org["ㄴ파트"]) == org["ㄴ파트"]     # 부서는 그대로


def test_folder_selection_is_read_as_its_department(session, org):
    """폴더가 권한 선택에 들어와도 그 부서 권한으로 해석한다(문서가 사라지지 않게)."""
    tree = OrgRepository(session).load_tree()
    toks = set(tree.readable_tokens([f"node:{org['대외공개']}"]))
    assert f"n:{org['ㄴ파트']}" in toks          # 파트원이 읽을 수 있어야 한다
    assert f"n:{org['대외공개']}" not in toks    # 폴더 토큰은 만들지 않는다


def test_department_tokens_exclude_folder_nodes(session, org):
    """부서를 고르면 하위 폴더 토큰은 안 생긴다(아무도 갖지 않는 토큰이라 무의미)."""
    tree = OrgRepository(session).load_tree()
    toks = set(tree.readable_tokens([f"node:{org['ㄴ파트']}"]))
    assert f"n:{org['ㄴ파트']}" in toks
    assert not any(t == f"n:{org['대외공개']}" or t == f"n:{org['파트내부']}" for t in toks)


def test_folder_in_document_does_not_leak_to_other_part(session, org):
    """폴더에 넣어도 열람 범위는 그 부서 그대로 — 형제 파트에 새지 않는다."""
    tree = OrgRepository(session).load_tree()
    toks = set(tree.readable_tokens([f"node:{org['파트내부']}"]))
    assert f"n:{org['ㅇ파트']}" not in toks


# ── 폴더 기본 권한(업로드 시 자동으로 채워지는 값) ──────────────────────────
def test_default_access_falls_back_to_department(session, org):
    repo = OrgRepository(session)
    assert repo.default_access_for(org["대외공개"]) == [f"node:{org['ㄴ파트']}"]


def test_default_access_can_be_set_per_folder(session, org):
    repo = OrgRepository(session)
    repo.set_default_access(org["대외공개"], [f"node:{org['team']}"])
    session.commit()
    assert repo.default_access_for(org["대외공개"]) == [f"node:{org['team']}"]
    # 형제 폴더는 영향 없음
    assert repo.default_access_for(org["파트내부"]) == [f"node:{org['ㄴ파트']}"]


def test_default_access_inherits_from_parent_folder(session, org):
    """하위 폴더에 값이 없으면 상위 폴더 설정을 물려받는다."""
    repo = OrgRepository(session)
    repo.set_default_access(org["대외공개"], [f"node:{org['team']}"])
    child = repo.create_node("2024년", FOLDER, parent_id=org["대외공개"])
    session.commit()
    assert repo.default_access_for(child.id) == [f"node:{org['team']}"]


# ── 업로드가 폴더 기본값을 쓴다 ─────────────────────────────────────────────
class _NoIndexer:
    client = collection = None

    def upsert(self, *a, **k):
        return 0

    def set_doc_payload(self, *a, **k):
        pass

    def delete_doc(self, *a, **k):
        pass


def test_upload_into_folder_prefills_its_default_access(session, org, tmp_path,
                                                        monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    repo = OrgRepository(session)
    repo.set_default_access(org["대외공개"], [f"node:{org['team']}"])
    session.commit()

    src = tmp_path / "안내문.txt"
    src.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")
    svc = ReviewService(session=session, llm=ExtractiveLLM(), llm_model="offline",
                        embedder=HashingEmbedder(), indexer=_NoIndexer())
    doc_id = svc.start_ingestion(str(src), ingested_by="u@co.com",
                                 folder_node_id=org["대외공개"])

    doc = DocumentRepository(session).get(doc_id)
    assert doc.governance.author_node_id == org["대외공개"]        # 저장 위치 = 폴더
    assert doc.governance.access_selections == [f"node:{org['team']}"]  # 권한 = 기본값
    stored = DocumentRepository(session).get_original_path(doc_id)
    assert "대외공개" in stored                                   # 디스크도 폴더 경로


def test_document_in_folder_is_visible_to_department_member(session, org, tmp_path,
                                                            monkeypatch):
    """폴더에 넣은 문서가 그 부서 사람 목록에서 사라지면 안 된다."""
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    src = tmp_path / "메모.txt"
    src.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")
    svc = ReviewService(session=session, llm=ExtractiveLLM(), llm_model="offline",
                        embedder=HashingEmbedder(), indexer=_NoIndexer())
    doc_id = svc.start_ingestion(str(src), ingested_by="u@co.com",
                                 folder_node_id=org["파트내부"])
    doc = svc.docs.get(doc_id)
    svc.submit_review(doc_id, governance=doc.governance)
    session.commit()

    users = UserRepository(session)
    users.set_memberships("member@co.com", [org["ㄴ파트"]], display_name="파트원")
    session.commit()
    vis = Visibility(read_tokens=frozenset(
        users.get_user_context("member@co.com").groups))
    titles = {d["doc_id"] for d in
              DocumentRepository(session).list_documents(visible_to=vis)}
    assert doc_id in titles


# ── 디스크 ↔ 화면 동기화 ────────────────────────────────────────────────────
def test_sync_creates_missing_directories(session, org, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    _registered, created = sync_with_disk(session)
    session.commit()
    tree = OrgRepository(session).load_tree()
    assert fs_dir(tree, org["대외공개"]).is_dir()
    assert created


def test_sync_registers_directories_made_on_the_server(session, org, tmp_path,
                                                       monkeypatch):
    """서버에서 직접 만든 디렉터리가 화면 폴더 목록에 등록된다."""
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    sync_with_disk(session)
    session.commit()

    tree = OrgRepository(session).load_tree()
    (fs_dir(tree, org["ㄴ파트"]) / "서버에서만든폴더").mkdir(parents=True)
    registered, _created = sync_with_disk(session)
    session.commit()

    assert any("서버에서만든폴더" in r for r in registered)
    names = {n.name: n for n in OrgRepository(session).list_nodes()}
    assert "서버에서만든폴더" in names
    new = names["서버에서만든폴더"]
    assert new.node_type == FOLDER and new.parent_id == org["ㄴ파트"]


def test_sync_is_idempotent(session, org, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    sync_with_disk(session)
    session.commit()
    registered, created = sync_with_disk(session)
    session.commit()
    assert registered == [] and created == []


# ── 권한(누가 폴더를 만들 수 있나)은 web/authz 에서 판정 ────────────────────
def test_org_node_of_unknown_is_none():
    tree = OrgTree([OrgNodeView(1, "떠도는폴더", FOLDER, None)])
    assert tree.org_node(1) is None          # 부서에 안 붙은 폴더는 권한 근거가 없다
    assert tree.readable_tokens(["node:1"]) == []   # → 아무 토큰도 만들지 않는다
