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
    st.header("📥 문서 업로드")
    uploaded = st.file_uploader(
        "인사 문서", type=["docx", "pptx", "xlsx", "pdf", "jpg", "png", "txt"])
    uploader_id = st.text_input("적재자 ID", value="admin")
    if uploaded is not None and st.button("적재 시작(자동 채움)"):
        suffix = Path(uploaded.name).suffix
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
            tf.write(uploaded.getvalue())
            tmp_path = tf.name
        # 원래 파일명 보존을 위해 이름 재구성
        named = Path(tempfile.gettempdir()) / uploaded.name
        Path(tmp_path).replace(named)
        try:
            doc_id = _service().start_ingestion(str(named), ingested_by=uploader_id)
            st.success(f"자동 채움 완료 → 검토 대기: {doc_id}")
        except DuplicateError as e:
            st.warning(f"이미 적재된 문서입니다: {e.existing_doc_id}")


# ── 메인: 검토 대기 목록 ──────────────────────────────────────────────────────
st.header("📝 적재 검토 (human-in-the-loop)")
svc = _service()
pending = svc.list_pending()

if not pending:
    st.info("검토 대기 중인 문서가 없습니다. 왼쪽에서 문서를 업로드하세요.")
    st.stop()

labels = {f"{p['filename']} · {p['status']} · {p['doc_id'][:8]}": p["doc_id"]
          for p in pending}
choice = st.selectbox("검토할 문서", list(labels.keys()))
doc_id = labels[choice]
view = svc.get_review(doc_id)

if view is None:
    st.error("문서를 불러올 수 없습니다.")
    st.stop()

st.caption(f"{view.source_filename} · {view.file_format} · "
           f"{view.page_count or '-'}p · 청크 {view.chunk_count}개 · 상태 {view.status}")

# LLM 자동 채움 요약(confidence)
if view.auto_filled:
    with st.expander("🤖 LLM 자동 채움 신뢰도", expanded=True):
        for a in view.auto_filled:
            st.write(f"- **{a['field']}**: {_conf_badge(a['confidence'])}")

col1, col2 = st.columns(2)

# ── 내용 분류(LLM 추론 → 사람 확인) ─────────────────────────────────────────
with col1:
    st.subheader("내용 분류")
    cls = view.classification
    doc_type = st.selectbox("doc_type", ENUM_OPTIONS["doc_type"],
                            index=ENUM_OPTIONS["doc_type"].index(cls["doc_type"]))
    language = st.selectbox("language", ENUM_OPTIONS["language"],
                            index=ENUM_OPTIONS["language"].index(cls["language"]))
    title = st.text_input("title_normalized", value=cls["title_normalized"] or "")
    summary = st.text_area("summary", value=cls["summary"] or "", height=80)
    department = st.text_input("department", value=cls["department"] or "")
    team = st.text_input("team", value=cls["team"] or "")
    topics = st.text_input("topics (쉼표 구분)", value=", ".join(cls["topics"]))

# ── 거버넌스(사람 필수) + 생애주기 ──────────────────────────────────────────
with col2:
    st.subheader("거버넌스 (필수) ⚠️")
    gov = view.governance
    sens_opts = ["(미설정)"] + ENUM_OPTIONS["sensitivity_level"]
    sens_idx = sens_opts.index(gov["sensitivity_level"]) if gov["sensitivity_level"] else 0
    sensitivity = st.selectbox("sensitivity_level *", sens_opts, index=sens_idx)
    contains_pii = st.radio("contains_pii *", ["(미설정)", "예", "아니오"],
                            index=0 if gov["contains_pii"] is None else (1 if gov["contains_pii"] else 2),
                            horizontal=True)
    pii_types = st.multiselect("pii_types", ENUM_OPTIONS["pii_types"],
                               default=gov["pii_types"])
    groups = st.multiselect("access_groups *", view.known_groups or [],
                            default=[g for g in gov["access_groups"] if g in (view.known_groups or [])])
    owner = st.text_input("owner *", value=gov["owner"] or "")

    st.subheader("생애주기")
    life = view.lifecycle
    lifecycle_status = st.selectbox(
        "status", ENUM_OPTIONS["lifecycle_status"],
        index=ENUM_OPTIONS["lifecycle_status"].index(life["lifecycle_status"]))
    effective = st.text_input("effective_date (YYYY-MM-DD)", value=life["effective_date"] or "")
    expiry = st.text_input("expiry_date (YYYY-MM-DD)", value=life["expiry_date"] or "")

# ── 제출 ─────────────────────────────────────────────────────────────────────
if st.button("✅ 검증 후 색인", type="primary"):
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
