"""영속 계층 테스트 (SQLite in-memory)."""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.models import Base
from app.db.persistence import persistent_hash_lookup, save_ingestion
from app.db.repositories import AuditRepository, DocumentRepository, UserRepository
from app.ingestion.intake import DuplicateError, intake
from app.ingestion.pipeline import apply_review, run_auto_stages
from app.schemas.enums import DocType, SensitivityLevel
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import GovernanceBlock


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


class FakeLLM:
    def complete_json(self, prompt, schema):
        return {
            "doc_type": {"value": "policy", "confidence": 0.95},
            "language": {"value": "ko", "confidence": 0.99},
            "status": {"value": "active", "confidence": 0.9},
        }


# ── Document 리포지토리 ──────────────────────────────────────────────────────
def test_document_roundtrip_and_hash_lookup(session, tmp_path: Path):
    p = tmp_path / "a.txt"
    p.write_text("연차는 15일", encoding="utf-8")
    repo = DocumentRepository(session)

    ctx = run_auto_stages(str(p), ingested_by="admin",
                          llm=FakeLLM(), llm_model="m")
    save_ingestion(repo, ctx)
    session.commit()

    doc_id = ctx.doc.identification.doc_id
    # hash_lookup 으로 중복 탐지 가능
    assert repo.hash_lookup(ctx.doc.identification.file_hash) == doc_id
    # 메타데이터 roundtrip
    loaded = repo.get(doc_id)
    assert loaded is not None
    assert loaded.classification.doc_type == DocType.POLICY
    assert loaded.identification.source_filename == "a.txt"
    # 상태 조회
    assert repo.list_by_status(IngestionStatus.PENDING_REVIEW) == [doc_id]


def test_dedup_blocks_second_ingest(session, tmp_path: Path):
    p = tmp_path / "dup.txt"
    p.write_text("동일 내용", encoding="utf-8")
    repo = DocumentRepository(session)

    ctx = run_auto_stages(str(p), ingested_by="admin", llm=FakeLLM(), llm_model="m")
    save_ingestion(repo, ctx)
    session.commit()

    # 같은 파일 재적재 시도 → 중복 탐지
    with pytest.raises(DuplicateError):
        intake(str(p), ingested_by="admin",
               hash_lookup=persistent_hash_lookup(repo))


def test_chunks_marked_indexed_after_index(session, tmp_path: Path):
    p = tmp_path / "policy.txt"
    p.write_text("연차 15일.\n\n병가 별도.", encoding="utf-8")
    repo = DocumentRepository(session)
    ctx = run_auto_stages(str(p), ingested_by="admin", llm=FakeLLM(), llm_model="m")

    gov = GovernanceBlock(sensitivity_level=SensitivityLevel.INTERNAL,
                          contains_pii=False, access_groups=["hr_core"],
                          owner="hr.manager")
    apply_review(ctx, governance=gov,
                 lifecycle_overrides={"status": "active"},
                 known_access_groups=["hr_core"])

    class FakeEmbedder:
        def embed(self, texts): return [[0.0] * 4 for _ in texts]

    class FakeIndexer:
        def upsert(self, vectors, payloads, ids): pass

    from app.ingestion.pipeline import index
    index(ctx, embedder=FakeEmbedder(), indexer=FakeIndexer())
    save_ingestion(repo, ctx)
    session.commit()

    from app.db.models import Chunk
    from sqlalchemy import select
    rows = list(session.execute(select(Chunk)).scalars())
    assert rows and all(r.indexed for r in rows)
    assert repo.get(ctx.doc.identification.doc_id) is not None


# ── User/Group 리포지토리 ───────────────────────────────────────────────────
def test_user_context_and_known_groups(session):
    repo = UserRepository(session)
    repo.upsert_group("hr_core", "인사팀 코어")
    repo.upsert_group("hr_lead")
    repo.upsert_user("u1", SensitivityLevel.CONFIDENTIAL, "홍길동")
    repo.add_user_to_group("u1", "hr_core")
    session.commit()

    ctx = repo.get_user_context("u1")
    assert ctx is not None
    assert ctx.clearance == SensitivityLevel.CONFIDENTIAL
    assert ctx.groups == frozenset({"hr_core"})
    assert set(repo.known_access_groups()) == {"hr_core", "hr_lead"}
    assert repo.get_user_context("ghost") is None


# ── Audit 리포지토리 ─────────────────────────────────────────────────────────
def test_audit_record(session):
    audit = AuditRepository(session)
    audit.record({"action": "query", "user_id": "u1",
                  "query_text": "연차 며칠?", "cited_doc_ids": ["d1"]})
    session.commit()

    from app.db.models import AuditLog
    from sqlalchemy import select
    rows = list(session.execute(select(AuditLog)).scalars())
    assert len(rows) == 1
    assert rows[0].action == "query"
    assert rows[0].event["cited_doc_ids"] == ["d1"]
