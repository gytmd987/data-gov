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


# ── 부서장(조직) 기반 문서 관리 권한 ────────────────────────────────────────
def manage_scope(session, user):
    """(is_admin, managed_node_ids) 반환.

    managed_node_ids = 이 사용자가 리더(부서장)인 노드들의 subtree 합집합.
    관리자면 전체 관리(scope 무제한). 리더가 아닌 사용자는 빈 집합.
    """
    from app.db.repositories import OrgRepository
    if is_admin(user):
        return True, set()
    org = OrgRepository(session)
    led = org.nodes_led_by(_email_of(user))
    if not led:
        return False, set()
    tree = org.load_tree()
    scope: set = set()
    for n in led:
        scope |= set(tree.subtree(n))
    return False, scope


def can_manage_doc(session, user, doc) -> bool:
    """관리자 또는 문서 작성부서가 내 관리 범위(subtree)에 드는 부서장."""
    is_adm, scope = manage_scope(session, user)
    if is_adm:
        return True
    node = doc.governance.author_node_id
    return node is not None and node in scope
