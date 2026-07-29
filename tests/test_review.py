"""검토 서비스 계층 테스트 (SQLite + fake 서비스)."""

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.ingestion.intake import DuplicateError
from app.review.service import ReviewService
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
            "doc_type": {"value": "report", "confidence": 0.95},
            "language": {"value": "ko", "confidence": 0.99},
            "status": {"value": "active", "confidence": 0.9},
            "summary": {"value": "요약", "confidence": 0.9},
            "keywords": ["연차"],
            "expected_qa": [{"question": "연차?", "answer": "15일"}],
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
    assert view.classification["doc_type"] == "report"
    assert view.classification["keywords"] == ["연차"]
    # 낮은 confidence 필드는 값이 안 채워지지만 auto_filled 기록에는 남는다
    fields = {a["field"] for a in view.auto_filled}
    assert "department" in fields
    assert view.classification["department"] is None


def test_submit_blocks_on_draft_status(session, tmp_path):
    svc = _service(session)
    doc_id = svc.start_ingestion(_make_file(tmp_path), ingested_by="admin")

    result = svc.submit_review(doc_id, governance=GovernanceBlock(),
                               lifecycle_overrides={"status": "draft"})
    assert not result.ok
    assert svc._status_of(doc_id) == IngestionStatus.BLOCKED.value


def test_submit_valid_indexes_and_leaves_pending_list(session, tmp_path):
    indexer = FakeIndexer()
    svc = _service(session, indexer=indexer)
    doc_id = svc.start_ingestion(_make_file(tmp_path), ingested_by="admin")

    # 접근 지정 없음 = 팀 전체("*")
    result = svc.submit_review(doc_id, governance=GovernanceBlock(author_name="hr.manager"),
                               lifecycle_overrides={"status": "active"})
    assert result.ok
    assert indexer.calls == 1
    assert svc._status_of(doc_id) == IngestionStatus.INDEXED.value
    # 색인 후 검토 대기 목록에서 사라짐
    assert all(p["doc_id"] != doc_id for p in svc.list_pending())
    # 팀 전체 문서 → payload 는 "*" 센티널
    payloads, _ = indexer.last
    assert payloads[0]["access_groups"] == ["*"]


def test_blocked_then_resubmit_indexes(session, tmp_path):
    indexer = FakeIndexer()
    svc = _service(session, indexer=indexer)
    doc_id = svc.start_ingestion(_make_file(tmp_path), ingested_by="admin")

    assert not svc.submit_review(doc_id, governance=GovernanceBlock(),
                                 lifecycle_overrides={"status": "draft"}).ok
    assert svc._status_of(doc_id) == IngestionStatus.BLOCKED.value

    result = svc.submit_review(doc_id, governance=GovernanceBlock(),
                               lifecycle_overrides={"status": "active"})
    assert result.ok
    assert svc._status_of(doc_id) == IngestionStatus.INDEXED.value


def test_duplicate_only_blocks_after_indexed(session, tmp_path):
    svc = _service(session)
    path = _make_file(tmp_path)
    d1 = svc.start_ingestion(path, ingested_by="admin")
    # 아직 검토 대기(색인 전) → 같은 파일 재업로드는 stale 교체로 허용
    d2 = svc.start_ingestion(path, ingested_by="admin")
    assert d2 != d1 and svc.docs.get(d1) is None
    # 등록 확정(색인) 후에는 중복 차단
    svc.submit_review(d2, governance=GovernanceBlock(),
                      lifecycle_overrides={"status": "active"})
    with pytest.raises(DuplicateError):
        svc.start_ingestion(path, ingested_by="admin")
