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
    """템플릿 공통 컨텍스트: 관리자 여부 + 업로드 개수 한도(화면 안내용)."""
    from django.conf import settings as dj

    return {
        "is_admin": is_admin(request.user),
        "upload_warn_files": getattr(dj, "UPLOAD_WARN_FILES", 20),
        "upload_max_files": getattr(dj, "DATA_UPLOAD_MAX_NUMBER_FILES", 50),
    }


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


def visibility_for(session, user):
    """이 사용자에게 관리 화면에서 보여도 되는 문서 범위(Visibility).

    목록·건수·문서 검색·상세 모두 이걸 통해서만 조회해야 한다. 화면에서 거르면
    페이징·건수가 어긋나고, 자동완성 같은 곁길로 제목이 새어 나간다.
    """
    from app.search.access import Visibility
    if is_admin(user):
        return Visibility.admin()
    ctx = domain_user_context(session, user)
    _, scope = manage_scope(session, user)
    return Visibility(
        read_tokens=frozenset(ctx.groups if ctx is not None else set()),
        manage_node_ids=frozenset(scope or set()))


def _token_readable(session, user, doc) -> bool:
    """문서 열람 토큰만으로 판정(관리 권한 무관). 생애주기는 보지 않는다.

    만료·대체는 '검색에 안 나온다'는 뜻이지 '못 읽는다'가 아니므로 권한 판정에서 뺀다.
    """
    if doc is None:
        return False
    return visibility_for(session, user).allows_tokens(doc.governance.access_tokens)


def can_read_doc(session, user, doc) -> bool:
    """열람 권한: 문서 열람 토큰과 겹치거나(공개 포함) 관리 대상이면 True."""
    if doc is None:
        return False
    return _token_readable(session, user, doc) or can_manage_doc(session, user, doc)


def can_manage_doc(session, user, doc) -> bool:
    """관리자 또는 문서 작성부서가 내 관리 범위(subtree)에 드는 부서장."""
    is_adm, scope = manage_scope(session, user)
    if is_adm:
        return True
    node = doc.governance.author_node_id
    return node is not None and node in scope


def can_edit_doc(session, user, doc) -> bool:
    """수정 권한: 관리자 · 부서장(내 subtree) · 본인 소속 부서(파트)의 문서.

    파트원도 자기 파트(소속 노드)의 문서는 직접 수정할 수 있다. 삭제는 별도(부서장만).
    단, **열람 권한이 없는 문서는 수정도 못 한다** — 같은 파트에 있다는 이유만으로
    권한이 좁혀진 문서를 열어볼 수 있으면 권한 설정이 무의미해진다.
    """
    if can_manage_doc(session, user, doc):
        return True
    from app.db.repositories import UserRepository
    node = doc.governance.author_node_id
    if node is None:
        return False
    mine = set(UserRepository(session).member_nodes(_email_of(user)))
    return node in mine and _token_readable(session, user, doc)


def can_manage_folder(session, user, parent_node_id) -> bool:
    """이 부서/폴더 아래에 하위 폴더를 만들거나 고칠 수 있는가.

    자기 소속 부서(및 그 아래 폴더)면 파트원도 가능하다 — 문서 정리는 실무자가 한다.
    부서장은 자기 관리 범위 전체, 관리자는 전부.
    """
    if parent_node_id is None:
        return False
    if is_admin(user):
        return True
    from app.db.repositories import OrgRepository, UserRepository
    is_adm, scope = manage_scope(session, user)
    if is_adm or parent_node_id in scope:
        return True
    tree = OrgRepository(session).load_tree()
    mine = set(UserRepository(session).member_nodes(_email_of(user)))
    # 내 소속 부서 자신이거나, 내 소속 부서 아래에 있는 폴더면 허용
    return parent_node_id in mine or bool(mine & set(tree.ancestors(parent_node_id)))


def can_delete_doc(session, user, doc) -> bool:
    """삭제 권한: 관리자 또는 문서 작성부서를 관리하는 부서장만(파트원 불가)."""
    return can_manage_doc(session, user, doc)


def can_review_doc(session, user, doc) -> bool:
    """검토(등록 승인) 권한: 업로더 본인 · 같은 파트 · 부서장 · 관리자.

    업로드한 사람이 AI가 채운 내용을 확인·수정하고 등록을 확정한다.
    """
    if doc.governance.author_id and _email_of(user) == doc.governance.author_id:
        return True
    return can_edit_doc(session, user, doc)
