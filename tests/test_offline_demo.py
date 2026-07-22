"""오프라인 데모 엔드투엔드 테스트 (외부 서비스 0개).

실제 파이프라인(적재→검토→색인→검색→답변) + 실제 Qdrant 엔진(in-memory)을 통과시켜
접근통제가 전 구간에서 작동함을 서비스 없이 검증한다.
"""

from datetime import date
from pathlib import Path

import pytest
from qdrant_client import QdrantClient
from sqlalchemy.orm import Session

from app.db.repositories import OrgRepository, UserRepository
from app.demo.offline import (
    build_offline_review_service,
    build_offline_search_pipeline,
    make_offline_engine,
)
from app.schemas.metadata import GovernanceBlock

SAMPLES = Path("samples")


@pytest.fixture
def wired():
    session = Session(make_offline_engine(), expire_on_commit=False)
    qdrant = QdrantClient(location=":memory:")
    # 조직도: People팀 > 인사파트 + 급여파트
    org = OrgRepository(session)
    team = org.create_node("People팀", "team")
    hr = org.create_node("인사파트", "part", parent_id=team.id)
    pay = org.create_node("급여파트", "part", parent_id=team.id)
    users = UserRepository(session)
    users.set_org("a", hr.id, "파트원")      # ANALYST: 인사파트 파트원
    users.set_org("l", team.id, "팀장")       # LEAD: 팀장(최상위 → 급여파트 상위)
    session.commit()

    svc = build_offline_review_service(session, qdrant)
    # 급여 문서만 급여파트로 제한, 나머지는 팀 전체
    govs = {"salary_bands_2026.txt": GovernanceBlock(access_selections=[f"node:{pay.id}"])}
    for p in sorted(SAMPLES.glob("*.txt")):
        doc_id = svc.start_ingestion(str(p), ingested_by="t")
        gov = govs.get(p.name, GovernanceBlock())
        result = svc.submit_review(doc_id, governance=gov,
                                   lifecycle_overrides={"status": "active"})
        assert result.ok

    pipe = build_offline_search_pipeline(session, qdrant)
    analyst = users.get_user_context("a")
    lead = users.get_user_context("l")
    return pipe, analyst, lead


def test_leave_query_answers_for_analyst(wired):
    pipe, analyst, _ = wired
    ans = pipe.answer("연차는 며칠인가요?", analyst, today=date(2026, 7, 20))
    files = {c.source_filename for c in ans.used_chunks}
    assert "annual_leave_policy.txt" in files
    assert "salary_bands_2026.txt" not in files
    assert ans.citations                      # 인용 존재


def test_salary_denied_for_analyst(wired):
    pipe, analyst, _ = wired
    ans = pipe.answer("부장 직급의 연봉 밴드는?", analyst, today=date(2026, 7, 20))
    files = {c.source_filename for c in ans.used_chunks}
    # 접근통제: 급여파트 문서가 인사파트 파트원에게는 배제
    assert "salary_bands_2026.txt" not in files
    assert "확인할 수 없습니다" in ans.text


def test_salary_allowed_for_lead(wired):
    pipe, _, lead = wired
    ans = pipe.answer("부장 직급의 연봉 밴드는?", lead, today=date(2026, 7, 20))
    files = {c.source_filename for c in ans.used_chunks}
    assert "salary_bands_2026.txt" in files    # 팀장(상위)은 열람 가능
    assert "115" in ans.text                  # 부장 밴드 하한
    assert ans.citations
