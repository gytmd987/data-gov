"""사용자 관리 페이지 — 직책·직무로 사용자를 만들면 권한이 자동 계산된다."""

from __future__ import annotations

import streamlit as st

from app import system_config
from app.db.repositories import UserRepository
from app.review.factory import new_session

st.set_page_config(page_title="사용자 관리", layout="wide")
st.header("👤 사용자 관리")
st.caption("직책·직무를 고르면 config/system.yaml 규칙에 따라 **열람 그룹과 등급이 자동 부여**됩니다.")

if "user_session" not in st.session_state:
    st.session_state.user_session = new_session()
session = st.session_state.user_session
users = UserRepository(session)

with st.form("new_user"):
    st.subheader("사용자 추가/수정")
    uid = st.text_input("사용자 ID")
    name = st.text_input("이름")
    col1, col2 = st.columns(2)
    position = col1.selectbox("직책", system_config.positions())
    job = col2.selectbox("직무", system_config.jobs())
    submitted = st.form_submit_button("저장", type="primary")
    if submitted and uid:
        groups, clearance = users.upsert_user_with_role(uid, position=position, job=job,
                                                        display_name=name or None)
        session.commit()
        st.success(f"'{uid}' 저장됨 → 그룹 {sorted(groups)} · 열람등급 {clearance}")

st.divider()
st.subheader("사용자 목록")
rows = users.list_users()
if rows:
    st.dataframe([
        {"ID": u["user_id"], "이름": u["display_name"], "직책": u["position"],
         "직무": u["job"], "열람등급": u["clearance"], "그룹": ", ".join(u["groups"])}
        for u in rows
    ], use_container_width=True)
else:
    st.info("등록된 사용자가 없습니다.")
