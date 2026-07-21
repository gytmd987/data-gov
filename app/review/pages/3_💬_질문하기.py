"""질문하기 페이지 — 권한에 따라 검색·답변하고 👍/👎 피드백."""

from __future__ import annotations

import os
from datetime import date

import streamlit as st

from app.db.repositories import FeedbackRepository, UserRepository
from app.review.factory import build_search_pipeline, new_session

st.set_page_config(page_title="질문하기", layout="wide")

from app.review.auth import require_login
login_email = require_login()   # 로그인한 본인으로 질의(권한 반영)

st.header("💬 질문하기")
st.caption("본인 권한으로 문서에 질문합니다. 접근 권한이 없는 문서는 답변 근거에서 제외됩니다.")

if "qa_session" not in st.session_state:
    st.session_state.qa_session = new_session()
session = st.session_state.qa_session
users = UserRepository(session)

if login_email:
    # SSO 로그인 → 본인 계정으로만 질의
    uid = login_email
    user = users.get_user_context(uid)
    if user is None:
        st.warning(f"'{uid}' 사용자 권한 정보가 없습니다. 관리자에게 사용자 등록을 요청하세요.")
        st.stop()
else:
    # 개발 모드 → 사용자 선택
    user_list = users.list_users()
    if not user_list:
        st.warning("사용자가 없습니다. '사용자 관리' 페이지에서 먼저 등록하세요.")
        st.stop()
    uid = st.selectbox("질문할 사용자", [u["user_id"] for u in user_list])
    user = users.get_user_context(uid)
st.caption(f"권한: 그룹 {', '.join(sorted(user.groups))} · 열람등급 {user.clearance.value}")

query = st.text_input("질문", placeholder="예: 연차는 며칠인가요?")
if st.button("질문하기", type="primary") and query:
    pipe = build_search_pipeline(session)
    ans = pipe.answer(query, user, today=date.today())
    session.commit()
    used_by_id = {c.chunk_id: c for c in ans.used_chunks}
    sources = []
    for cit in ans.citations:
        uc = used_by_id.get(cit.chunk_id)
        sources.append({"marker": cit.marker, "label": cit.label,
                        "doc_id": cit.doc_id, "page": cit.page_no,
                        "passage": uc.text if uc else ""})
    st.session_state.last_qa = {
        "query": query, "text": ans.text, "cited": [c.doc_id for c in ans.citations],
        "sources": sources}

qa = st.session_state.get("last_qa")
if qa:
    st.markdown(f"**답변:** {qa['text']}")

    if qa["sources"]:
        st.subheader("📎 출처 (답변 근거)")
        from app.db.repositories import DocumentRepository
        docs_repo = DocumentRepository(session)
        for s in qa["sources"]:
            page = f" · p.{s['page']}" if s["page"] else ""
            with st.expander(f"[{s['marker']}] {s['label']}{page}"):
                st.write(s["passage"] or "(본문 미리보기 없음)")
                path = docs_repo.get_original_path(s["doc_id"]) if s["doc_id"] else None
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        st.download_button("📄 원본 파일 열기/다운로드", f.read(),
                                           file_name=os.path.basename(path),
                                           key=f"dl_{s['marker']}")
                else:
                    st.caption("원본 파일이 보관돼 있지 않습니다.")
    else:
        st.caption("권한 내 근거 문서를 찾지 못했습니다.")

    st.write("이 답변이 정확한가요?")
    c1, c2 = st.columns(2)
    if c1.button("👍 정확함"):
        FeedbackRepository(session).record(qa["query"], "up", user_id=uid,
                                           answer_text=qa["text"], cited_doc_ids=qa["cited"])
        session.commit()
        st.success("피드백 감사합니다.")
    with c2:
        note = st.text_input("👎 틀렸다면 무엇이 틀렸는지/정답", key="fb_note")
        if st.button("👎 틀림 제출"):
            FeedbackRepository(session).record(qa["query"], "down", user_id=uid,
                answer_text=qa["text"], note=note or None, cited_doc_ids=qa["cited"])
            session.commit()
            st.warning("오답 피드백이 기록되었습니다(관리자 검토 대상).")
