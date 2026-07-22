"""중앙 설정(config/system.yaml) + 조직 배정 테스트."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import system_config
from app.db.models import Base
from app.db.repositories import OrgRepository, UserRepository


def test_vocab_loaded():
    assert "report" in system_config.doc_types()
    assert "unknown" in system_config.doc_types()


def test_labels_and_departments():
    assert system_config.label("report") == "보고서"
    assert system_config.label("*") == "팀 전체"
    assert system_config.label("docx") == "워드"
    assert system_config.label("없는값") == "없는값"      # 매핑 없으면 원값
    assert system_config.departments()                    # 부서 목록 존재


def test_org_roles():
    roles = system_config.org_roles()
    assert roles == ["팀장", "그룹장", "파트장", "파트원"]


def test_admins():
    assert "admin@company.com" in system_config.admin_emails()


# ── 조직 배정 → 접근 컨텍스트 ────────────────────────────────────────────────
@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


def test_set_org_builds_context(session):
    org = OrgRepository(session)
    team = org.create_node("People팀", "team")
    part = org.create_node("파트A", "part", parent_id=team.id)
    session.commit()

    users = UserRepository(session)
    users.set_org("kim", part.id, "파트장", display_name="김파트장")
    session.commit()

    ctx = users.get_user_context("kim")
    assert ctx is not None
    assert ctx.groups == frozenset({f"n:{part.id}", f"h:{part.id}"})
