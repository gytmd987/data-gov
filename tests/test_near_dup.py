"""유사(개정판) 자동 탐지 테스트 (offline: HashingEmbedder + in-memory Qdrant)."""

from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from app.demo.offline import build_offline_review_service, make_offline_engine
from app.db.repositories import UserRepository
from app.schemas.enums import SensitivityLevel
from app.schemas.metadata import GovernanceBlock


@pytest.fixture
def svc(tmp_path):
    session = Session(make_offline_engine(), expire_on_commit=False)
    qdrant = QdrantClient(location=":memory:")
    UserRepository(session).upsert_group("hr_core")
    session.commit()
    return build_offline_review_service(session, qdrant), tmp_path


def _ingest_and_index(svc, tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    doc_id = svc.start_ingestion(str(p), ingested_by="t")
    gov = GovernanceBlock(sensitivity_level=SensitivityLevel.INTERNAL,
                          contains_pii=False, access_groups=["hr_core"], owner="mgr")
    svc.submit_review(doc_id, governance=gov, lifecycle_overrides={"status": "active"})
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
