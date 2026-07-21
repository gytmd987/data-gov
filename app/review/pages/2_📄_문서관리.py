"""문서 관리 페이지 — 색인된 문서 조회·수정·버전연결·보관·삭제."""

from __future__ import annotations

import streamlit as st

from app.review.factory import build_document_manager
from app.review.service import ENUM_OPTIONS
from app.schemas.enums import DocStatus, SensitivityLevel
from app.schemas.metadata import GovernanceBlock

st.set_page_config(page_title="문서 관리", layout="wide")

from app.review.auth import require_admin
require_admin()   # 관리자 전용

st.header("📄 문서 관리")
st.caption("이미 등록된 문서를 관리합니다. 메타데이터 수정, **다른 문서의 새 버전으로 연결**, 보관/삭제.")

mgr = build_document_manager()

q = st.text_input("검색(파일명·제목)")
docs = mgr.list_documents(text=q or None)
if not docs:
    st.info("문서가 없습니다.")
    st.stop()

# 목록 표
st.dataframe([
    {"파일": d["filename"], "유형": d["doc_type"], "민감도": d["sensitivity_level"],
     "상태": d["lifecycle_status"], "그룹": ", ".join(d["access_groups"] or []),
     "책임자": d["owner"], "대체됨": bool(d["superseded_by"])}
    for d in docs
], use_container_width=True)

labels = {f"{d['filename']} · {d['lifecycle_status']} · {d['doc_id'][:8]}": d["doc_id"]
          for d in docs}
sel = st.selectbox("관리할 문서 선택", list(labels.keys()))
doc_id = labels[sel]
doc = mgr.get(doc_id)

st.divider()
col1, col2 = st.columns(2)

# ── 메타데이터 수정 ──────────────────────────────────────────────────────────
with col1:
    st.subheader("메타데이터 수정")
    gov = doc.governance
    sens_opts = ENUM_OPTIONS["sensitivity_level"]
    sensitivity = st.selectbox("민감도", sens_opts,
        index=sens_opts.index(gov.sensitivity_level.value) if gov.sensitivity_level else 0)
    groups = st.text_input("열람 그룹(쉼표 구분)", value=", ".join(gov.access_groups))
    owner = st.text_input("책임자", value=gov.owner or "")
    status_opts = ENUM_OPTIONS["lifecycle_status"]
    status = st.selectbox("상태", status_opts,
        index=status_opts.index(doc.lifecycle.status.value))

    if st.button("💾 저장 (검색에 즉시 반영)", type="primary"):
        new_gov = GovernanceBlock(
            sensitivity_level=SensitivityLevel(sensitivity),
            contains_pii=gov.contains_pii, pii_types=gov.pii_types,
            access_groups=[g.strip() for g in groups.split(",") if g.strip()],
            owner=owner or None)
        mgr.update_metadata(doc_id, governance=new_gov,
                            lifecycle_overrides={"status": status})
        st.success("저장 완료. Qdrant 검색 필터에 반영되었습니다.")
        st.rerun()

# ── 버전 연결 / 보관 / 삭제 ──────────────────────────────────────────────────
with col2:
    st.subheader("버전 연결 (개정판 처리)")
    st.caption("이 문서를 **다른 문서의 새 버전**으로 지정하면, 지정된 옛 문서는 검색에서 제외됩니다.")
    others = {f"{d['filename']}": d["doc_id"] for d in docs if d["doc_id"] != doc_id}
    if others:
        old_label = st.selectbox("이 문서가 대체할 옛 문서", ["(선택 안 함)"] + list(others.keys()))
        if old_label != "(선택 안 함)" and st.button("🔗 이 문서를 새 버전으로 지정"):
            mgr.supersede(others[old_label], doc_id)
            st.success(f"'{old_label}' 을(를) 이 문서의 이전 버전으로 처리했습니다(검색 제외).")
            st.rerun()

    st.subheader("보관 / 삭제")
    if st.button("🗄️ 보관(archived) — 검색 제외, 기록 유지"):
        mgr.archive(doc_id)
        st.success("보관 처리했습니다.")
        st.rerun()
    if st.checkbox("정말 영구 삭제하려면 체크"):
        if st.button("🗑️ 영구 삭제", type="secondary"):
            mgr.delete(doc_id, hard=True)
            st.warning("영구 삭제했습니다.")
            st.rerun()
