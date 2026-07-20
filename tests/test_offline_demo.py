"""오프라인 데모 엔드투엔드 테스트 (외부 서비스 0개).

실제 파이프라인(적재→검토→색인→검색→답변) + 실제 Qdrant 엔진(in-memory)을 통과시켜
접근통제가 전 구간에서 작동함을 서비스 없이 검증한다.
"""

from datetime import date
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from app.db.repositories import UserRepository
from app.demo.offline import (
    build_offline_review_service,
    build_offline_search_pipeline,
    make_offline_engine,
)
from app.schemas.enums import PiiType, SensitivityLevel
from app.schemas.metadata import GovernanceBlock
from app.search.access import UserContext

SAMPLES = Path("samples")


@pytest.fixture
def wired():
    session = Session(make_offline_engine(), expire_on_commit=False)
    qdrant = QdrantClient(location=":memory:")
    users = UserRepository(session)
    users.upsert_group("hr_core")
    users.upsert_group("payroll")
    session.commit()

    svc = build_offline_review_service(session, qdrant)
    govs = {
        "salary_bands_2026.txt": GovernanceBlock(
            sensitivity_level=SensitivityLevel.RESTRICTED, contains_pii=True,
            pii_types=[PiiType.SALARY], access_groups=["payroll"], owner="lead"),
    }
    for p in sorted(SAMPLES.glob("*.txt")):
        doc_id = svc.start_ingestion(str(p), ingested_by="t")
        gov = govs.get(p.name, GovernanceBlock(
            sensitivity_level=SensitivityLevel.INTERNAL, contains_pii=False,
            access_groups=["hr_core"], owner="mgr"))
        result = svc.submit_review(doc_id, governance=gov,
                                   lifecycle_overrides={"status": "active"})
        assert result.ok

    pipe = build_offline_search_pipeline(session, qdrant)
    return pipe


ANALYST = UserContext("a", frozenset(["hr_core"]), SensitivityLevel.INTERNAL)
LEAD = UserContext("l", frozenset(["hr_core", "payroll"]), SensitivityLevel.RESTRICTED)


def test_leave_query_answers_for_analyst(wired):
    ans = wired.answer("연차는 며칠인가요?", ANALYST, today=date(2026, 7, 20))
    files = {c.source_filename for c in ans.used_chunks}
    assert "annual_leave_policy.txt" in files
    assert "salary_bands_2026.txt" not in files
    assert ans.citations                      # 인용 존재


def test_salary_denied_for_analyst(wired):
    ans = wired.answer("부장 직급의 연봉 밴드는?", ANALYST, today=date(2026, 7, 20))
    files = {c.source_filename for c in ans.used_chunks}
    # 접근통제: 급여 문서가 후보에서 배제
    assert "salary_bands_2026.txt" not in files
    assert "확인할 수 없습니다" in ans.text


def test_salary_allowed_for_lead(wired):
    ans = wired.answer("부장 직급의 연봉 밴드는?", LEAD, today=date(2026, 7, 20))
    files = {c.source_filename for c in ans.used_chunks}
    assert "salary_bands_2026.txt" in files
    assert "115" in ans.text                  # 부장 밴드 하한
    assert ans.citations
