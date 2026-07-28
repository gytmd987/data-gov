"""문서 연관 자동 감지 + RelationRepository 테스트."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.repositories import RelationRepository
from app.relations.detect import detect_related, normalize_stem


def test_normalize_stem_strips_markers_and_version():
    assert normalize_stem("평가보고서.docx") == "평가보고서"
    assert normalize_stem("평가보고서_별첨1.xlsx") == "평가보고서"
    assert normalize_stem("report_v2.pdf") == "report"
    assert normalize_stem("report 2026.pdf") == "report"


def test_detect_by_filename_stem():
    existing = [
        {"doc_id": "rep", "filename": "평가보고서.docx", "title": "평가 보고서"},
        {"doc_id": "other", "filename": "채용계획.docx", "title": "채용 계획"},
    ]
    found = detect_related("xls", "평가보고서_별첨1.xlsx", [], existing)
    ids = {f["doc_id"] for f in found}
    assert ids == {"rep"}                     # 파일명 어간 일치만


def test_detect_by_mention_and_similarity():
    existing = [
        {"doc_id": "rep", "filename": "b.docx", "title": "2026 평가 보고서"},
        {"doc_id": "sim", "filename": "c.docx", "title": "무관"},
    ]
    found = detect_related(
        "xls", "급여표.xlsx",
        mentions=["2026 평가 보고서"],           # 본문 언급
        existing=existing,
        sim_candidates=[{"doc_id": "sim", "score": 0.72},   # 중간대 → 연관
                        {"doc_id": "dup", "score": 0.95}])  # 중복대 → 제외
    by = {f["doc_id"]: f["reason"] for f in found}
    assert by.get("rep") == "본문 언급"
    assert by.get("sim") == "내용 유사"
    assert "dup" not in by                     # 중복(개정판)대는 연관에서 제외


# ── RelationRepository ───────────────────────────────────────────────────────
@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


def test_classify_relation_maps_and_falls_back():
    from app.relations.classify import RELATION_LABELS, classify_relation

    class OkLLM:
        def complete_json(self, prompt, schema):
            return {"relation": "related", "reason": "별첨 자료"}

    class BadLLM:
        def complete_json(self, prompt, schema):
            raise RuntimeError("llm down")

    r = classify_relation(OkLLM(), "새", "요약", "기존", "요약2")
    assert r["relation"] == "related" and r["reason"] == "별첨 자료"
    assert "related" in RELATION_LABELS
    # 실패 시 revision 으로 폴백
    assert classify_relation(BadLLM(), "a", "b", "c", "d")["relation"] == "revision"


def test_relation_repo_symmetric_and_dedup(session):
    rel = RelationRepository(session)
    rel.link("d1", "d2", source="auto", reason="파일명")
    rel.link("d2", "d1", source="auto")           # 역방향 = 같은 쌍 → 중복 안 생김
    session.commit()
    assert len(rel.related_ids("d1")) == 1
    assert rel.related_ids("d1")[0]["doc_id"] == "d2"
    assert rel.related_ids("d2")[0]["doc_id"] == "d1"

    rel.unlink("d1", "d2")
    session.commit()
    assert rel.related_ids("d1") == []
