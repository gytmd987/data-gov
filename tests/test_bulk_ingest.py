"""과거 문서 일괄 반입 스크립트.

핵심은 **폴더 구조 → 조직 노드(작성부서·열람 권한) 매핑**이 정확한 것이다.
여기가 틀리면 수천 건이 잘못된 권한으로 들어가고 되돌리기가 고통스럽다.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.repositories import DocumentRepository, OrgRepository
from app.demo.offline import ExtractiveLLM, HashingEmbedder
from app.ingestion.enrichment import ReadError
from app.review.service import ReviewService
from app.schemas.enums import DocType
from app.schemas.ingestion import IngestionStatus
from scripts import bulk_ingest as bi


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def org(session):
    """People팀 > ㅁ그룹 > (ㄴ파트, ㅇ파트)"""
    repo = OrgRepository(session)
    team = repo.create_node("People팀", "team")
    m = repo.create_node("ㅁ그룹", "group", parent_id=team.id)
    n = repo.create_node("ㄴ파트", "part", parent_id=m.id)
    o = repo.create_node("ㅇ파트", "part", parent_id=m.id)
    session.commit()
    return {"team": team.id, "ㅁ": m.id, "ㄴ": n.id, "ㅇ": o.id}


def _tree(session):
    return OrgRepository(session).load_tree()


def _make_tree_of_files(base, layout: dict[str, list[str]]):
    """layout = {'People팀/ㅁ그룹/ㄴ파트': ['a.txt', ...]}"""
    for rel, names in layout.items():
        d = base / rel if rel else base
        d.mkdir(parents=True, exist_ok=True)
        for name in names:
            (d / name).write_text(f"{name} 내용. 연차는 15일이며 인사팀에 신청한다.",
                                  encoding="utf-8")


# ── 폴더 → 조직 노드 매핑 ────────────────────────────────────────────────────
def test_folder_path_maps_to_org_node(session, org, tmp_path):
    _make_tree_of_files(tmp_path, {
        "People팀/ㅁ그룹/ㄴ파트": ["연차규정.txt"],
        "People팀/ㅁ그룹/ㅇ파트": ["평가지침.txt"],
        "People팀/ㅁ그룹": ["그룹공지.txt"],
    })
    tree = _tree(session)
    items = bi.make_plan(tmp_path, bi.build_node_index(tree), (), tree)
    got = {it.path.name: it.node_id for it in items}
    assert got == {"연차규정.txt": org["ㄴ"], "평가지침.txt": org["ㅇ"],
                   "그룹공지.txt": org["ㅁ"]}


def test_root_node_lets_folder_start_mid_tree(session, org, tmp_path):
    """반입 폴더가 조직도 중간(ㅁ그룹)부터 시작하는 경우."""
    _make_tree_of_files(tmp_path, {"ㄴ파트": ["규정.txt"], "": ["그룹문서.txt"]})
    tree = _tree(session)
    root = tuple(tree.name_path(org["ㅁ"]))
    items = bi.make_plan(tmp_path, bi.build_node_index(tree), root, tree)
    got = {it.path.name: it.node_id for it in items}
    assert got == {"규정.txt": org["ㄴ"], "그룹문서.txt": org["ㅁ"]}


def test_unknown_subfolder_falls_back_to_nearest_parent(session, org, tmp_path):
    """조직도에 없는 하위 폴더(연도 등)는 가장 가까운 상위 부서로 붙는다."""
    _make_tree_of_files(tmp_path, {"People팀/ㅁ그룹/ㄴ파트/2024년/1분기": ["보고서.txt"]})
    tree = _tree(session)
    items = bi.make_plan(tmp_path, bi.build_node_index(tree), (), tree)
    assert items[0].node_id == org["ㄴ"]


def test_unmatched_folder_is_flagged_not_guessed(session, org, tmp_path):
    """조직도와 전혀 안 맞는 폴더는 임의로 추측하지 않고 미매칭으로 남긴다."""
    _make_tree_of_files(tmp_path, {"어디부서": ["정체불명.txt"]})
    tree = _tree(session)
    items = bi.make_plan(tmp_path, bi.build_node_index(tree), (), tree)
    assert items[0].node_id is None
    assert items[0].node_label == bi.UNFILED_LABEL


# ── 반입 대상 선별 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,expected", [
    ("규정.docx", True), ("명단.xlsx", True), ("공지.pdf", True), ("메모.txt", True),
    ("~$규정.docx", False),      # 오피스 임시 파일
    (".DS_Store", False),        # 숨김 파일
    ("Thumbs.db", False),
    ("기안.hwp", False),         # 미지원 형식
    ("압축.zip", False),
])
def test_candidate_filtering(tmp_path, name, expected):
    assert bi.is_candidate(tmp_path / name) is expected


def test_plan_skips_non_candidates(session, org, tmp_path):
    _make_tree_of_files(tmp_path, {
        "People팀/ㅁ그룹/ㄴ파트": ["규정.txt", "~$규정.docx", "기안.hwp"]})
    tree = _tree(session)
    items = bi.make_plan(tmp_path, bi.build_node_index(tree), (), tree)
    assert [it.path.name for it in items] == ["규정.txt"]


# ── 실제 적재(오프라인 fake 로) ──────────────────────────────────────────────
class _NoIndexer:
    client = None
    collection = None

    def upsert(self, *a, **k):
        return 0

    def set_doc_payload(self, *a, **k):
        pass

    def delete_doc(self, *a, **k):
        pass


@pytest.fixture
def service(session, monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "storage_dir", str(tmp_path / "originals"))
    svc = ReviewService(session=session, llm=ExtractiveLLM(), llm_model="offline",
                        embedder=HashingEmbedder(), indexer=_NoIndexer())
    monkeypatch.setattr(bi, "_service", lambda: svc)
    return svc


def _item(path, node_id):
    return bi.PlanItem(path=path, rel_dir=(), node_id=node_id, node_label="x")


def test_auto_confirm_indexes_with_folder_permissions(session, org, service, tmp_path):
    src = tmp_path / "연차규정.txt"
    src.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")

    kind, _ = bi.ingest_one(_item(src, org["ㄴ"]), "bulk", auto_confirm=True)
    assert kind == "ok"

    repo = DocumentRepository(session)
    doc_id = repo.list_documents()[0]["doc_id"]
    assert repo.get_status(doc_id) == IngestionStatus.INDEXED.value
    doc = repo.get(doc_id)
    assert doc.governance.author_node_id == org["ㄴ"]           # 작성부서 = 폴더
    assert doc.governance.access_selections == [f"node:{org['ㄴ']}"]   # 권한 = 폴더


def test_without_auto_confirm_waits_for_review(session, org, service, tmp_path):
    src = tmp_path / "연차규정.txt"
    src.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")

    kind, _ = bi.ingest_one(_item(src, org["ㄴ"]), "bulk", auto_confirm=False)
    assert kind == "ok"
    repo = DocumentRepository(session)
    doc_id = repo.list_documents()[0]["doc_id"]
    assert repo.get_status(doc_id) == IngestionStatus.PENDING_REVIEW.value


def test_already_ingested_file_is_skipped(session, org, service, tmp_path):
    """이어하기: 같은 파일을 다시 돌리면 건너뛴다."""
    src = tmp_path / "연차규정.txt"
    src.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")

    assert bi.ingest_one(_item(src, org["ㄴ"]), "bulk", auto_confirm=True)[0] == "ok"
    assert bi.ingest_one(_item(src, org["ㄴ"]), "bulk", auto_confirm=True)[0] == "skipped"
    assert len(DocumentRepository(session).list_documents()) == 1


def test_unknown_doc_type_becomes_other(session, org, service, tmp_path, monkeypatch):
    """AI가 종류를 못 정하면 '기타'로 등록한다(미분류로 남기지 않는다)."""
    src = tmp_path / "정체불명.txt"
    src.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")
    doc_id = service.start_ingestion(str(src), ingested_by="bulk",
                                     folder_node_id=org["ㄴ"])
    doc = service.docs.get(doc_id)
    doc.classification.doc_type = DocType("unknown")   # AI가 확신 못 한 상태
    service.docs.upsert_document(doc, IngestionStatus.PENDING_REVIEW)
    session.commit()

    bi.confirm(service, doc_id)
    assert DocumentRepository(session).get(doc_id).classification.doc_type.value == "other"


def test_read_failure_is_reported_not_raised(session, org, service, tmp_path,
                                             monkeypatch):
    """읽기 실패는 예외를 던지지 않고 사유를 돌려준다(전체 반입이 멈추면 안 된다)."""
    src = tmp_path / "스캔본.txt"
    src.write_text("x", encoding="utf-8")

    def boom(*a, **k):
        raise ReadError("파일 내용을 읽지 못했습니다(추출 실패).")

    monkeypatch.setattr(service, "start_ingestion", boom)
    kind, note = bi.ingest_one(_item(src, org["ㄴ"]), "bulk", auto_confirm=True)
    assert kind == "failed" and "읽지 못했습니다" in note


def test_unexpected_error_is_reported_not_raised(session, org, service, tmp_path,
                                                 monkeypatch):
    """예상 못 한 오류(vLLM 일시 장애 등)도 그 한 건만 실패로 남는다."""
    src = tmp_path / "문서.txt"
    src.write_text("연차 휴가는 15일이며 인사팀에 신청한다.", encoding="utf-8")

    def boom(*a, **k):
        raise RuntimeError("vLLM 503")

    monkeypatch.setattr(service, "start_ingestion", boom)
    kind, note = bi.ingest_one(_item(src, org["ㄴ"]), "bulk", auto_confirm=True)
    assert kind == "failed" and "vLLM 503" in note
