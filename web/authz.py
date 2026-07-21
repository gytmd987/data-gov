"""권한 헬퍼: 관리자 판정 + 도메인 사용자 컨텍스트 매핑."""

from __future__ import annotations

from django.contrib.auth.decorators import user_passes_test

from app import system_config


def _email_of(user) -> str:
    return (user.email or user.username or "").strip()


def is_admin(user) -> bool:
    if not user.is_authenticated:
        return False
    return user.is_staff or _email_of(user) in system_config.admin_emails()


admin_required = user_passes_test(is_admin, login_url="/accounts/login/")


def admin_context(request):
    """템플릿 컨텍스트: is_admin (상단 메뉴 노출용)."""
    return {"is_admin": is_admin(request.user)}


def domain_user_context(session, user):
    """Django 로그인 사용자 → RAG 권한 컨텍스트(UserContext). 없으면 None."""
    from app.db.repositories import UserRepository
    return UserRepository(session).get_user_context(_email_of(user))
