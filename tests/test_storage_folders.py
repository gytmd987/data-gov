"""폴더(=조직노드) 기반 저장·분류·검색."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.repositories import DocumentRepository, OrgRepository
from app.manage.storage import UNFILED, fs_dir, place, relocate_subtree
from app.review.service import ReviewService
from app.demo.offline import ExtractiveLLM, HashingEmbedder
from app.schemas.metadata import doc_level_payload
from app.search.access import AccessPolicy, UserContext


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
    grp = repo.create_node("채용그룹", "group", parent_id=team.id)
    part = repo.create_node("인터뷰파트", "part", parent_id=grp.id)
    session.commit()
    return team, grp, part


class _NoIndexer:
    client = None
    collection = None

    def upsert(self, *a, **k):
        return 0

    def set_doc_payload(self, *a, **k):
        pass

    def delete_doc(self, *a, **k):
        pass


def _service(session):
    return ReviewService(session=session, llm=ExtractiveLLM(), llm_model="offline",
                         embedder=HashingEmbedder(), indexer=_NoIndexer())


def _upload(session, tmp_path, name="연차 규정.txt", folder=None):
    p = tmp_path / name
    p.write_text("연차 휴가는 15일이며 신청은 인사팀에 합니다.", encoding="utf-8")
    return _service(session).start_ingestion(str(p), ingested_by="admin@company.com",
                                             folder_node_id=folder)


# ── 경로 계산 ────────────────────────────────────────────────────────────────
def test_fs_dir_mirrors_org_path(session, org, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    team, grp, part = org
    tree = OrgRepository(session).load_tree()
    assert fs_dir(tree, part.id).parts[-3:] == ("People팀", "채용그룹", "인터뷰파트")
    assert fs_dir(tree, None).name == UNFILED       # 노드 미지정 → _미분류


# ── 업로드: 폴더가 작성부서·권한·저장경로를 정한다 ──────────────────────────
def test_upload_folder_sets_defaults_and_path(session, org, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    _team, _grp, part = org
    doc_id = _upload(session, tmp_path, folder=part.id)

    doc = DocumentRepository(session).get(doc_id)
    assert doc.governance.author_node_id == part.id           # 작성부서 = 폴더
    assert doc.classification.department == "인터뷰파트"
    assert doc.governance.access_selections == [f"node:{part.id}"]   # 부서 전체 열람

    stored = DocumentRepository(session).get_original_path(doc_id)
    assert "인터뷰파트" in stored and Path(stored).exists()


def test_upload_without_folder_goes_unfiled(session, org, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    doc_id = _upload(session, tmp_path, name="미분류 문서.txt")
    stored = DocumentRepository(session).get_original_path(doc_id)
    assert UNFILED in stored


# ── 폴더 변경 시 파일 이동 ───────────────────────────────────────────────────
def test_place_moves_file_between_folders(session, org, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    team, grp, part = org
    doc_id = _upload(session, tmp_path, folder=part.id)
    before = DocumentRepository(session).get_original_path(doc_id)

    after = place(session, doc_id, grp.id, "연차 규정", ".txt")
    session.commit()
    assert "채용그룹" in after and "인터뷰파트" not in after
    assert Path(after).exists() and not Path(before).exists()


# ── 노드 이름 변경 → subtree 재배치(멱등) ───────────────────────────────────
def test_relocate_subtree_after_rename(session, org, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    team, _grp, part = org
    doc_id = _upload(session, tmp_path, folder=part.id)

    OrgRepository(session).rename_node(part.id, "면접파트")
    session.commit()
    moved = relocate_subtree(session, team.id)
    session.commit()
    assert moved == 1
    after = DocumentRepository(session).get_original_path(doc_id)
    assert "면접파트" in after and Path(after).exists()
    # 멱등: 다시 실행하면 이동 없음
    assert relocate_subtree(session, team.id) == 0


# ── 검색 폴더 스코프(상위 선택 → 하위 포함) ────────────────────────────────
def test_folder_scope_filter_includes_subtree(session, org, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    team, grp, part = org
    doc_id = _upload(session, tmp_path, folder=part.id)
    payload = doc_level_payload(DocumentRepository(session).get(doc_id))
    payload["status"] = "archived"
    assert payload["author_node_id"] == part.id

    user = UserContext(user_id="u", groups=frozenset({f"n:{part.id}"}))
    tree = OrgRepository(session).load_tree()
    # 상위(팀) 폴더 선택 → subtree 포함이라 통과
    pol = AccessPolicy.for_user(user, folder_node_ids=frozenset(tree.subtree(team.id)))
    assert pol.allows(payload)
    # 형제 폴더(그룹만) 선택 → 제외
    other = OrgRepository(session).create_node("타그룹", "group", parent_id=team.id)
    session.commit()
    pol2 = AccessPolicy.for_user(user, folder_node_ids=frozenset(tree.subtree(other.id)))
    assert not pol2.allows(payload)
