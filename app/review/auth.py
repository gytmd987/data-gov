"""Streamlit SSO(OIDC) 로그인 + 역할(관리자) 게이트.

- SSO가 설정돼 있으면(.streamlit/secrets.toml 의 [auth]) 로그인을 강제한다.
- 설정이 없으면 '개발 모드'로 게이트 없이 통과(현재 로컬 테스트가 계속 동작).
- 관리자 여부는 config/system.yaml 의 permissions.admins(이메일)로 판단.

설정 방법: docs/인증_SSO.md
"""

from __future__ import annotations

from typing import Optional

import streamlit as st

from app import system_config


def _auth_configured() -> bool:
    """Streamlit 로그인 기능 + [auth] secrets 가 준비됐는지."""
    if not hasattr(st, "login") or not hasattr(st, "user"):
        return False
    try:
        _ = st.user.is_logged_in   # [auth] secrets 없으면 예외
        return True
    except Exception:
        return False


def current_email() -> Optional[str]:
    if not _auth_configured():
        return None
    return st.user.email if st.user.is_logged_in else None


def require_login() -> Optional[str]:
    """로그인 강제. 인증 미설정(개발)이면 게이트 없이 None 반환."""
    if not _auth_configured():
        st.sidebar.info("🔓 인증 미설정(개발 모드) · SSO 설정: docs/인증_SSO.md")
        return None
    if not st.user.is_logged_in:
        st.title("🔒 로그인이 필요합니다")
        st.button("SSO로 로그인", type="primary", on_click=st.login)
        st.stop()
    with st.sidebar:
        st.caption(f"👤 {st.user.email}")
        st.button("로그아웃", on_click=st.logout)
    return st.user.email


def require_admin() -> Optional[str]:
    """관리자 전용 화면 게이트. 인증 미설정이면 개발 모드로 통과."""
    email = require_login()
    if email is None:
        return None   # 개발 모드
    admins = system_config.admin_emails()
    if not admins or email not in admins:
        st.error("관리자 전용 화면입니다. config/system.yaml 의 permissions.admins 에 "
                 "본인 이메일을 추가하세요.")
        st.stop()
    return email
