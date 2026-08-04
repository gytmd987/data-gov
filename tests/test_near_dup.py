"""유사(개정판) 자동 탐지 테스트 (offline: HashingEmbedder + in-memory Qdrant)."""

from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from app.demo.offline import build_offline_review_service, make_offline_engine
from app.schemas.metadata import GovernanceBlock


@pytest.fixture
def svc(tmp_path):
    session = Session(make_offline_engine(), expire_on_commit=False)
    qdrant = QdrantClient(location=":memory:")
    return build_offline_review_service(session, qdrant), tmp_path


def _ingest_and_index(svc, tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    doc_id = svc.start_ingestion(str(p), ingested_by="t")
    svc.submit_review(doc_id, governance=GovernanceBlock(),
                      lifecycle_overrides={"status": "active"})
    return doc_id


def test_similar_document_flagged(svc):
    service, tmp_path = svc
    a = _ingest_and_index(service, tmp_path, "leave_v1.txt",
                          "연차 휴가 규정. 1년 근속 시 15일의 연차를 부여한다. 미사용분은 수당 지급.")
    # 거의 동일한 개정판 업로드 → 유사 후보로 A가 잡혀야 함
    b = service.start_ingestion(str(_write(tmp_path, "leave_v2.txt",
        "연차 휴가 규정. 1년 근속 시 15일의 연차를 부여한다. 미사용분은 수당으로 지급.")),
        ingested_by="t")
    view = service.get_review(b)
    cand_ids = {c["doc_id"] for c in view.similar_candidates}
    assert a in cand_ids


def test_dissimilar_document_not_flagged(svc):
    service, tmp_path = svc
    _ingest_and_index(service, tmp_path, "leave.txt",
                      "연차 휴가 규정. 15일의 연차를 부여한다.")
    b = service.start_ingestion(str(_write(tmp_path, "parking.txt",
        "주차장 이용 안내. 지하 3층까지 주차 가능하며 방문객은 등록이 필요하다.")),
        ingested_by="t")
    view = service.get_review(b)
    assert view.similar_candidates == []


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ── 제목·파일명으로도 검색되게(문서 단위 검색 청크) ─────────────────────────
def _doc_for_qa():
    from datetime import datetime, timezone
    from app.schemas.enums import DocType, FileFormat
    from app.schemas.metadata import (ClassificationBlock, DocumentMetadata,
                                      IdentificationBlock)
    return DocumentMetadata(
        identification=IdentificationBlock(
            doc_id="d1", source_filename="연차규정_최종본.docx",
            file_format=FileFormat.DOCX, file_hash="h" * 8,
            ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ingested_by="u@co.com"),
        classification=ClassificationBlock(
            doc_type=DocType("regulation"), title_normalized="(24-0315) 연차 휴가 규정",
            department="인사파트", summary="연차 15일 부여.",
            keywords=["연차", "휴가"],
            expected_qa=[{"question": "연차는 며칠?", "answer": "15일"}]))


def test_qa_chunk_includes_title_filename_and_department():
    """본문 청크엔 제목·파일명이 없어서, 이름으로 찾는 질문이 안 잡히던 문제."""
    from app.ingestion.pipeline import build_qa_chunk_text
    text = build_qa_chunk_text(_doc_for_qa())
    assert "연차규정_최종본.docx" in text     # 원본 파일명
    assert "(24-0315) 연차 휴가 규정" in text  # 제목
    assert "인사파트" in text                  # 작성부서
    assert "연차 15일 부여." in text           # 기존 요약도 유지
    assert "Q. 연차는 며칠?" in text


def test_qa_chunk_empty_when_nothing_to_say():
    from app.ingestion.pipeline import build_qa_chunk_text
    from app.schemas.metadata import DocumentMetadata, IdentificationBlock
    from app.schemas.enums import FileFormat
    from datetime import datetime, timezone
    doc = DocumentMetadata(identification=IdentificationBlock(
        doc_id="d", source_filename="", file_format=FileFormat.TXT,
        file_hash="h" * 8, ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ingested_by="u"))
    assert build_qa_chunk_text(doc) == ""
