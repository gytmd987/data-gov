"""거버넌스 스키마·상태머신·검증/차단 로직 회귀 테스트."""

from datetime import date, datetime

import pytest

from app import system_config
from app.schemas.enums import (
    ChunkType,
    DocStatus,
    DocType,
    FileFormat,
    PiiType,
    SensitivityLevel,
)
from app.schemas.ingestion import (
    IngestionStatus,
    IngestionTransitionError,
    assert_transition,
    can_transition,
)
from app.schemas.metadata import (
    ChunkMetadata,
    DocumentMetadata,
    GovernanceBlock,
    IdentificationBlock,
    LifecycleBlock,
)
from app.governance.validator import validate_governance


def _base_doc(**gov_overrides) -> DocumentMetadata:
    gov = {
        "sensitivity_level": SensitivityLevel.RESTRICTED,
        "contains_pii": True,
        "pii_types": [PiiType.SALARY],
        "access_groups": ["hr_core"],
        "owner": "hr.manager",
    }
    gov.update(gov_overrides)
    return DocumentMetadata(
        identification=IdentificationBlock(
            doc_id="doc-1",
            source_filename="2026_salary.xlsx",
            file_format=FileFormat.XLSX,
            file_hash="abc123",
            ingested_at=datetime(2026, 7, 16, 9, 0, 0),
            ingested_by="admin",
            page_count=3,
        ),
        governance=GovernanceBlock(**gov),
        lifecycle=LifecycleBlock(status=DocStatus.ACTIVE, effective_date=date(2026, 1, 1)),
    )


# ── 상태 머신 ────────────────────────────────────────────────────────────────
def test_happy_path_transitions():
    chain = [
        IngestionStatus.UPLOADED,
        IngestionStatus.PARSED,
        IngestionStatus.AUTO_ENRICHED,
        IngestionStatus.PENDING_REVIEW,
        IngestionStatus.VALIDATED,
        IngestionStatus.INDEXED,
    ]
    for cur, nxt in zip(chain, chain[1:]):
        assert can_transition(cur, nxt)
        assert_transition(cur, nxt)  # 예외 없어야 함


def test_illegal_transition_raises():
    with pytest.raises(IngestionTransitionError):
        assert_transition(IngestionStatus.UPLOADED, IngestionStatus.INDEXED)


def test_blocked_can_return_to_review():
    assert can_transition(IngestionStatus.PENDING_REVIEW, IngestionStatus.BLOCKED)
    assert can_transition(IngestionStatus.BLOCKED, IngestionStatus.PENDING_REVIEW)


def test_indexed_is_terminal():
    assert not can_transition(IngestionStatus.INDEXED, IngestionStatus.PENDING_REVIEW)


# ── 거버넌스 검증 / 차단 ─────────────────────────────────────────────────────
def test_valid_document_passes():
    result = validate_governance(_base_doc(), known_access_groups=["hr_core", "hr_lead"])
    assert result.ok
    assert result.as_status() == IngestionStatus.VALIDATED


def test_missing_required_fields_blocks():
    doc = _base_doc(sensitivity_level=None, owner=None)
    result = validate_governance(doc)
    assert not result.ok
    assert "governance.sensitivity_level" in result.missing_fields
    assert "governance.owner" in result.missing_fields
    assert result.as_status() == IngestionStatus.BLOCKED


def test_empty_access_groups_blocks():
    result = validate_governance(_base_doc(access_groups=[]))
    assert not result.ok
    assert "governance.access_groups" in result.missing_fields


def test_pii_flag_without_types_errors():
    result = validate_governance(_base_doc(contains_pii=True, pii_types=[]))
    assert not result.ok
    assert any("pii_types" in e for e in result.errors)


def test_unknown_access_group_errors():
    result = validate_governance(_base_doc(access_groups=["ghost"]), known_access_groups=["hr_core"])
    assert not result.ok
    assert any("access_groups" in e for e in result.errors)


def test_draft_status_cannot_index():
    doc = _base_doc()
    doc.lifecycle.status = DocStatus.DRAFT
    result = validate_governance(doc)
    assert not result.ok
    assert any("draft" in e for e in result.errors)


# ── 청크 payload 상속 (하드 필터 대비) ──────────────────────────────────────
def test_chunk_payload_inherits_access_control():
    doc = _base_doc()
    chunk = ChunkMetadata(
        chunk_id="doc-1::0",
        parent_doc_id="doc-1",
        chunk_type=ChunkType.TABLE,
        section_title="2026 급여표",
        page_no=1,
    )
    payload = chunk.to_qdrant_payload(doc)
    assert payload["access_groups"] == ["hr_core"]
    assert payload["sensitivity_rank"] == system_config.sensitivity_rank(SensitivityLevel.RESTRICTED) == 3
    assert payload["status"] == "active"
    assert payload["chunk_type"] == "table"
