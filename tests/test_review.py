"""검토 서비스 계층 테스트 (SQLite + fake 서비스)."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.repositories import UserRepository
from app.ingestion.intake import DuplicateError
from app.review.service import ReviewService
from app.schemas.enums import SensitivityLevel
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import GovernanceBlock


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


class FakeLLM:
    def complete_json(self, prompt, schema):
        return {
            "doc_type": {"value": "policy", "confidence": 0.95},
            "language": {"value": "ko", "confidence": 0.99},
            "status": {"value": "active", "confidence": 0.9},
            "department": {"value": "인사팀", "confidence": 0.4},  # 낮음
        }


class FakeEmbedder:
    def embed(self, texts):
        return [[0.0] * 4 for _ in texts]


class FakeIndexer:
    def __init__(self):
        self.calls = 0

    def upsert(self, vectors, payloads, ids):
        self.calls += 1
        self.last = (payloads, ids)


def _service(session, indexer=None):
    return ReviewService(
        session=session, llm=FakeLLM(), llm_model="m",
        embedder=FakeEmbedder(), indexer=indexer or FakeIndexer())


def _make_file(tmp_path: Path, name="policy.txt", text="연차 15일.\n\n병가 별도."):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_start_ingestion_lists_pending_and_review(session, tmp_path):
    svc = _service(session)
    doc_id = svc.start_ingestion(_make_file(tmp_path), ingested_by="admin")

    pending = svc.list_pending()
    assert any(p["doc_id"] == doc_id for p in pending)

    view = svc.get_review(doc_id)
    assert view is not None
    assert view.chunk_count == 2
    assert view.classification["doc_type"] == "policy"
    # 낮은 confidence 필드는 값이 안 채워지지만 auto_filled 기록에는 남는다
    fields = {a["field"] for a in view.auto_filled}
    assert "department" in fields
    assert view.classification["department"] is None


def test_submit_blocks_on_missing_governance(session, tmp_path):
    svc = _service(session)
    doc_id = svc.start_ingestion(_make_file(tmp_path), ingested_by="admin")

    gov = GovernanceBlock(sensitivity_level=SensitivityLevel.INTERNAL,
                          contains_pii=False, access_groups=["hr_core"], owner=None)
    result = svc.submit_review(doc_id, governance=gov,
                               lifecycle_overrides={"status": "active"})
    assert not result.ok
    assert "governance.owner" in result.missing_fields
    assert svc._status_of(doc_id) == IngestionStatus.BLOCKED.value


def test_submit_valid_indexes_and_leaves_pending_list(session, tmp_path):
    UserRepository(session).upsert_group("hr_core")
    indexer = FakeIndexer()
    svc = _service(session, indexer=indexer)
    doc_id = svc.start_ingestion(_make_file(tmp_path), ingested_by="admin")

    gov = GovernanceBlock(sensitivity_level=SensitivityLevel.INTERNAL,
                          contains_pii=False, access_groups=["hr_core"],
                          owner="hr.manager")
    result = svc.submit_review(doc_id, governance=gov,
                               lifecycle_overrides={"status": "active"})
    assert result.ok
    assert indexer.calls == 1
    assert svc._status_of(doc_id) == IngestionStatus.INDEXED.value
    # 색인 후 검토 대기 목록에서 사라짐
    assert all(p["doc_id"] != doc_id for p in svc.list_pending())
    # 색인 payload에 접근통제 상속
    payloads, _ = indexer.last
    assert payloads[0]["access_groups"] == ["hr_core"]


def test_blocked_then_resubmit_indexes(session, tmp_path):
    UserRepository(session).upsert_group("hr_core")
    indexer = FakeIndexer()
    svc = _service(session, indexer=indexer)
    doc_id = svc.start_ingestion(_make_file(tmp_path), ingested_by="admin")

    bad = GovernanceBlock(sensitivity_level=None, contains_pii=None,
                          access_groups=[], owner=None)
    assert not svc.submit_review(doc_id, governance=bad,
                                 lifecycle_overrides={"status": "active"}).ok
    assert svc._status_of(doc_id) == IngestionStatus.BLOCKED.value

    good = GovernanceBlock(sensitivity_level=SensitivityLevel.CONFIDENTIAL,
                           contains_pii=True, pii_types=[],
                           access_groups=["hr_core"], owner="hr.manager")
    # contains_pii=True 인데 pii_types 비어 → 여전히 차단
    assert not svc.submit_review(doc_id, governance=good,
                                 lifecycle_overrides={"status": "active"}).ok

    from app.schemas.enums import PiiType
    good.pii_types = [PiiType.SALARY]
    result = svc.submit_review(doc_id, governance=good,
                               lifecycle_overrides={"status": "active"})
    assert result.ok
    assert svc._status_of(doc_id) == IngestionStatus.INDEXED.value


def test_duplicate_ingestion_raises(session, tmp_path):
    svc = _service(session)
    path = _make_file(tmp_path)
    svc.start_ingestion(path, ingested_by="admin")
    with pytest.raises(DuplicateError):
        svc.start_ingestion(path, ingested_by="admin")
