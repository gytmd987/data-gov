"""중앙 설정(config/system.yaml) + 권한(직책×직무) 테스트."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import system_config
from app.db.models import Base
from app.db.repositories import UserRepository


def test_vocab_loaded():
    assert "policy" in system_config.doc_types()
    assert system_config.sensitivity_levels() == [
        "public", "internal", "confidential", "restricted"]
    assert "salary" in system_config.pii_types()
    assert "hr_core" in system_config.access_groups()


def test_sensitivity_rank_by_order():
    assert system_config.sensitivity_rank("public") == 0
    assert system_config.sensitivity_rank("restricted") == 3
    assert system_config.sensitivity_rank("nonexistent") == -1


def test_required_fields():
    req = system_config.required_governance_fields()
    assert "owner" in req and "access_groups" in req


# ── 권한: 직책×직무 조합 ─────────────────────────────────────────────────────
def test_same_job_different_position():
    # 같은 '급여' 직무라도 직책에 따라 clearance 다름
    _, c_low = system_config.resolve_access("사원", "급여")
    _, c_high = system_config.resolve_access("부장", "급여")
    assert c_low == "confidential"
    assert c_high == "restricted"


def test_same_position_different_job():
    # 같은 '부장' 직책이라도 직무에 따라 그룹 다름
    g_pay, _ = system_config.resolve_access("부장", "급여")
    g_rec, _ = system_config.resolve_access("부장", "채용")
    assert "payroll" in g_pay and "recruiting" not in g_pay
    assert "recruiting" in g_rec and "payroll" not in g_rec


def test_base_group_always_present():
    g, _ = system_config.resolve_access("사원", "일반")
    assert "hr_core" in g


# ── 역할 기반 사용자 생성 ────────────────────────────────────────────────────
@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


def test_upsert_user_with_role(session):
    users = UserRepository(session)
    groups, clearance = users.upsert_user_with_role("kim", position="부장", job="급여",
                                                     display_name="김부장")
    session.commit()
    assert "payroll" in groups and clearance == "restricted"

    ctx = users.get_user_context("kim")
    assert ctx is not None
    assert "payroll" in ctx.groups and "hr_core" in ctx.groups
    assert ctx.clearance.value == "restricted"
