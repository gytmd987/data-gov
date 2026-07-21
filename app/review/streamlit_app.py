"""적재 검토 Streamlit UI (human-in-the-loop).

실행: streamlit run app/review/streamlit_app.py

흐름:
  1) 사이드바에서 파일 업로드 → 자동 단계(파싱·청킹·LLM 자동채움) 실행 → PENDING_REVIEW
  2) 검토 대기 목록에서 문서 선택
  3) LLM 자동 채움(confidence 표시) 확인·수정 + 거버넌스 필수 필드 입력
  4) 제출 → 검증 통과 시 색인(INDEXED), 미충족 시 차단(BLOCKED) 사유 표시

로직은 app.review.service.ReviewService 에 있고, 이 파일은 화면만 담당한다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

from app.ingestion.intake import DuplicateError
from app.review.factory import build_service
from app.review.service import ENUM_OPTIONS
from app.schemas.enums import PiiType, SensitivityLevel
from app.schemas.metadata import GovernanceBlock

st.set_page_config(page_title="인사 RAG 적재 검토", layout="wide")


def _service():
    if "service" not in st.session_state:
        st.session_state.service = build_service()
    return st.session_state.service


def _conf_badge(conf: float) -> str:
    if conf >= 0.8:
        return f"🟢 {conf:.2f}"
    if conf >= 0.6:
        return f"🟡 {conf:.2f}"
    return f"🔴 {conf:.2f} (확인 필요)"


# ── 사이드바: 업로드 ──────────────────────────────────────────────────────────
with st.sidebar:
    st.header("📥 문서 올리기")
    st.caption("문서를 올리면 AI가 내용을 읽어 항목을 자동으로 채웁니다. "
               "그다음 오른쪽에서 사람이 확인·보완하면 검색에 등록됩니다.")
    uploaded = st.file_uploader(
        "인사 문서 선택", type=["docx", "pptx", "xlsx", "pdf", "jpg", "png", "txt"])
    uploader_id = st.text_input("올린 사람 ID", value="admin")
    if uploaded is not None and st.button("올리기 (AI 자동 채움)"):
        suffix = Path(uploaded.name).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
            tf.write(uploaded.getvalue())
            tmp_path = tf.name
        # 원래 파일명 보존을 위해 이름 재구성
        named = Path(tempfile.gettempdir()) / uploaded.name
        Path(tmp_path).replace(named)
        try:
            doc_id = _service().start_ingestion(str(named), ingested_by=uploader_id)
            st.success(f"자동 채움 완료 → 검토 대기 목록에 추가됨")
        except DuplicateError as e:
            st.warning(f"이미 등록된 문서입니다(내용 동일). 기존 문서: {e.existing_doc_id[:8]}")


# ── 메인: 검토 대기 목록 ──────────────────────────────────────────────────────
st.header("📝 문서 검토 및 등록")
st.caption("AI가 채운 항목을 확인하고, 접근·보안 정보를 채워 검색에 등록하는 화면입니다.")
svc = _service()
pending = svc.list_pending()

if not pending:
    st.info("검토할 문서가 없습니다. 왼쪽에서 문서를 올려주세요.")
    st.stop()

labels = {f"{p['filename']} · 상태:{p['status']}": p["doc_id"] for p in pending}
choice = st.selectbox("검토할 문서 선택", list(labels.keys()))
doc_id = labels[choice]
view = svc.get_review(doc_id)

if view is None:
    st.error("문서를 불러올 수 없습니다.")
    st.stop()

st.caption(f"파일: {view.source_filename} · 형식: {view.file_format} · "
           f"{view.page_count or '-'}쪽 · 조각 {view.chunk_count}개 · 현재 상태: {view.status}")

# LLM 자동 채움 요약(confidence)
if view.auto_filled:
    with st.expander("🤖 AI 자동 채움 신뢰도 (🟢높음 🟡보통 🔴확인필요)", expanded=True):
        for a in view.auto_filled:
            st.write(f"- **{a['field']}**: {_conf_badge(a['confidence'])}")

col1, col2 = st.columns(2)

# ── 내용 분류(LLM 추론 → 사람 확인) ─────────────────────────────────────────
with col1:
    st.subheader("① 내용 분류")
    st.caption("AI가 채운 값입니다. 맞는지 확인하고 필요하면 고쳐주세요.")
    cls = view.classification
    doc_type = st.selectbox("문서 유형", ENUM_OPTIONS["doc_type"],
                            index=ENUM_OPTIONS["doc_type"].index(cls["doc_type"]))
    language = st.selectbox("언어", ENUM_OPTIONS["language"],
                            index=ENUM_OPTIONS["language"].index(cls["language"]))
    title = st.text_input("제목", value=cls["title_normalized"] or "")
    summary = st.text_area("요약", value=cls["summary"] or "", height=80)
    department = st.text_input("담당 부서", value=cls["department"] or "")
    team = st.text_input("담당 팀", value=cls["team"] or "")
    topics = st.text_input("주제 태그 (쉼표로 구분)", value=", ".join(cls["topics"]))

# ── 거버넌스(사람 필수) + 생애주기 ──────────────────────────────────────────
with col2:
    st.subheader("② 접근·보안 (필수) ⚠️")
    st.caption("이 항목을 채워야 색인됩니다. 누가 볼 수 있는지·민감한지 정하는 곳입니다.")
    gov = view.governance
    sens_opts = ["(미설정)"] + ENUM_OPTIONS["sensitivity_level"]
    sens_idx = sens_opts.index(gov["sensitivity_level"]) if gov["sensitivity_level"] else 0
    sensitivity = st.selectbox("민감도 등급 *", sens_opts, index=sens_idx,
                               help="낮음: public → 높음: restricted(급여·평가 등)")
    contains_pii = st.radio("개인정보 포함? *", ["(미설정)", "예", "아니오"],
                            index=0 if gov["contains_pii"] is None else (1 if gov["contains_pii"] else 2),
                            horizontal=True)
    pii_types = st.multiselect("개인정보 유형", ENUM_OPTIONS["pii_types"],
                               default=gov["pii_types"])
    groups = st.multiselect("열람 가능 그룹 *", view.known_groups or [],
                            default=[g for g in gov["access_groups"] if g in (view.known_groups or [])],
                            help="이 문서를 볼 수 있는 접근 그룹")
    owner = st.text_input("문서 책임자(owner) *", value=gov["owner"] or "")

    st.subheader("③ 생애주기")
    life = view.lifecycle
    lifecycle_status = st.selectbox(
        "상태", ENUM_OPTIONS["lifecycle_status"],
        index=ENUM_OPTIONS["lifecycle_status"].index(life["lifecycle_status"]),
        help="active 여야 검색에 노출됩니다")
    effective = st.text_input("시행일 (YYYY-MM-DD)", value=life["effective_date"] or "")
    expiry = st.text_input("만료일 (YYYY-MM-DD)", value=life["expiry_date"] or "")

# ── 제출 ─────────────────────────────────────────────────────────────────────
if st.button("✅ 확인 완료 → 검증 후 색인", type="primary"):
    governance = GovernanceBlock(
        sensitivity_level=None if sensitivity == "(미설정)" else SensitivityLevel(sensitivity),
        contains_pii=None if contains_pii == "(미설정)" else (contains_pii == "예"),
        pii_types=[PiiType(p) for p in pii_types],
        access_groups=groups,
        owner=owner or None,
    )
    cls_over = {
        "doc_type": doc_type, "language": language,
        "title_normalized": title or None, "summary": summary or None,
        "department": department or None, "team": team or None,
        "topics": [t.strip() for t in topics.split(",") if t.strip()],
    }
    life_over = {
        "status": lifecycle_status,
        "effective_date": effective or None,
        "expiry_date": expiry or None,
    }
    result = svc.submit_review(doc_id, governance=governance,
                               classification_overrides=cls_over,
                               lifecycle_overrides=life_over, do_index=True)
    if result.ok:
        st.success("✅ 검증 통과 → 색인 완료(INDEXED). 검색에 노출됩니다.")
    else:
        st.error("⛔ 거버넌스 미충족 → 적재 차단(BLOCKED)")
        if result.missing_fields:
            st.write("**누락 필수 필드:**", ", ".join(result.missing_fields))
        for err in result.errors:
            st.write("- ", err)
