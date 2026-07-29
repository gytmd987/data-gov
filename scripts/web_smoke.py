"""Django 웹 스모크 (외부 서비스 0개, WEB_OFFLINE=1).

migrate → 계정/권한 시드 → 문서 적재·색인 → 채팅(RAG ON/OFF)·피드백·권한을
django.test.Client로 검증한다.

    python -m scripts.web_smoke
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ["WEB_OFFLINE"] = "1"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "web.hrrag.settings")

import django  # noqa: E402

django.setup()

from django.contrib.auth.models import User as DjUser  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402

from web import bridge  # noqa: E402


def main() -> int:
    call_command("migrate", verbosity=0, interactive=False)

    # Django 계정: 관리자 + 일반 사용자
    admin, _ = DjUser.objects.get_or_create(
        username="admin@company.com", defaults={"email": "admin@company.com",
                                                "is_staff": True})
    staffer, _ = DjUser.objects.get_or_create(
        username="hong@company.com", defaults={"email": "hong@company.com"})

    # 조직도 + 사용자 배정 + 샘플 문서 색인 (오프라인)
    session = bridge.open_session()
    from app.db.repositories import OrgRepository, UserRepository
    from app.schemas.metadata import GovernanceBlock
    org = OrgRepository(session)
    team = org.create_node("People팀", "team")
    part = org.create_node("인사파트", "part", parent_id=team.id)
    users = UserRepository(session)
    users.set_org("hong@company.com", part.id, "파트원")
    users.set_org("admin@company.com", team.id, "팀장")
    session.commit()

    svc = bridge.get_review_service(session)
    doc_id = svc.start_ingestion("samples/annual_leave_policy.txt", ingested_by="smoke")
    r = svc.submit_review(doc_id, governance=GovernanceBlock(),   # 팀 전체
                          lifecycle_overrides={"status": "active"})
    assert r.ok, f"색인 실패: {r.missing_fields}{r.errors}"
    session.close()

    c = Client()
    c.force_login(staffer)

    # 1) RAG ON
    resp = c.post("/chat/send", json.dumps({"text": "연차는 며칠인가요?", "use_rag": True}),
                  content_type="application/json")
    d = resp.json()
    assert resp.status_code == 200, d
    assert d["sources"], "RAG 출처가 비어 있음"
    conv_id = d["conversation_id"]
    print(f"[RAG ON ] {d['text'][:60]}…  출처 {len(d['sources'])}건 ✅")

    # 2) RAG OFF (멀티턴, 같은 대화)
    resp = c.post("/chat/send", json.dumps({"conversation_id": conv_id,
                  "text": "고마워!", "use_rag": False}),
                  content_type="application/json")
    d2 = resp.json()
    assert resp.status_code == 200 and d2["conversation_id"] == conv_id
    print(f"[RAG OFF] {d2['text'][:60]}…  (일반 답변) ✅")

    # 3) 대화 이력
    hist = c.get(f"/chat/history/{conv_id}").json()["messages"]
    assert len(hist) == 4, f"이력 {len(hist)}건"
    print(f"[이력   ] 메시지 {len(hist)}건 저장 ✅")

    # 4) 피드백
    resp = c.post("/chat/feedback", json.dumps({"query": "연차는 며칠인가요?",
                  "rating": "down", "note": "테스트", "answer": d["text"]}),
                  content_type="application/json")
    assert resp.json()["ok"]
    print("[피드백 ] 기록 ✅")

    # 5) 콘솔 권한: 일반 사용자는 차단(redirect), 관리자는 200
    assert c.get("/console/review/").status_code == 302
    c.force_login(admin)
    assert c.get("/console/review/").status_code == 200
    assert c.get("/console/docs/").status_code == 200
    assert c.get("/console/users/").status_code == 200
    print("[콘솔   ] 일반 사용자 차단 · 관리자 접근 ✅")

    # 6) 원본 다운로드 권한: 문서 그룹(hr_core)인 hong은 허용 경로 확인(404=원본 미보관 or 200)
    c.force_login(staffer)
    resp = c.get(f"/docs/original/{doc_id}")
    assert resp.status_code in (200, 404)
    print(f"[다운로드] 권한 검증 경로 동작(status={resp.status_code}) ✅")

    # 7) 문서 등록/관리는 '문서' 한 탭으로 통합 — /submit/ 은 문서 탭으로 이동(하위호환)
    assert c.get("/submit/").status_code == 302
    assert c.get("/console/docs/").status_code == 200
    print("[등록   ] 문서 탭 통합 · /submit/ → /console/docs/ 이동 ✅")

    # 8) 관리 콘솔: 필터·페이징·만료 정리. 보관 문서는 기본 검색에 노출됨.
    c.force_login(admin)
    assert c.get("/console/docs/?status=archived&doc_type=report&page=1").status_code == 200
    assert c.post("/console/docs/sweep", {}).status_code == 302
    # 시드 문서를 '보관'으로 바꿔도 기본 검색에 그대로 노출되는지 확인용으로 보관 처리
    from app.schemas.enums import DocStatus
    s_ar = bridge.open_session()
    try:
        bridge.get_document_manager(s_ar).set_status(doc_id, DocStatus.ARCHIVED)
    finally:
        s_ar.close()
    print("[콘솔+  ] 필터·페이징·만료 정리 · 보관 전환 ✅")

    # 9) 보관 문서 = 기본 검색 노출 / 만료 문서 = 만료포함일 때만 노출
    c.force_login(staffer)
    r_arch = c.post("/chat/send", json.dumps({"text": "연차는 며칠인가요?", "use_rag": True}),
                    content_type="application/json").json()
    assert any(s["doc_id"] == doc_id for s in r_arch.get("sources", [])), \
        "보관 문서가 기본 검색에 나와야 함"
    # 만료로 전환 → 기본 검색 제외, 만료 포함 검색에만 노출
    s_ex = bridge.open_session()
    try:
        bridge.get_document_manager(s_ex).set_status(doc_id, DocStatus.EXPIRED)
    finally:
        s_ex.close()
    r_def = c.post("/chat/send", json.dumps({"text": "연차는 며칠인가요?", "use_rag": True}),
                   content_type="application/json").json()
    assert not any(s["doc_id"] == doc_id for s in r_def.get("sources", [])), "만료 문서가 기본 검색에 노출됨"
    d3 = c.post("/chat/send", json.dumps({"text": "연차는 며칠인가요?", "use_rag": True,
                "include_past": True}), content_type="application/json").json()
    assert any(s.get("is_past") and s["doc_id"] == doc_id for s in d3.get("sources", [])), \
        "만료 포함 검색에서 만료 문서(is_past)가 나와야 함"
    # 다시 보관으로 되돌려 이후 섹션에 영향 없게
    s_rb = bridge.open_session()
    try:
        bridge.get_document_manager(s_rb).set_status(doc_id, DocStatus.ARCHIVED)
    finally:
        s_rb.close()
    print("[검색상태] 보관=검색노출 · 만료=만료포함시에만 노출 ✅")

    # 10) 조직도 관리(관리자 전용): 팀>그룹>파트 생성 + 사용자 배정
    c.force_login(admin)
    assert c.get("/console/org/").status_code == 200
    from app.db.repositories import OrgRepository
    s2 = bridge.open_session()
    try:
        org = OrgRepository(s2)
        t = org.create_node("People팀", "team")
        g = org.create_node("채용그룹", "group", parent_id=t.id)
        p = org.create_node("인터뷰파트", "part", parent_id=g.id)
        s2.commit()
        tid, gid, pid = t.id, g.id, p.id
    finally:
        s2.close()
    # 사용자 관리 화면에서 노드 배정(복수 소속 가능, 역할 없음)
    resp = c.post("/console/users/", {"user_id": "hong@company.com", "name": "홍파트원",
                  "org_node_id": [str(pid), str(gid)]})
    assert resp.status_code == 302
    body = c.get("/console/org/").content.decode()
    assert "People팀" in body and "인터뷰파트" in body and "홍파트원" in body
    print("[조직도 ] 팀>그룹>파트 생성 · 복수 소속 배정 · 계층 표시 ✅")

    # 10b) 다중 소속 반영 + 조직도에서 부서장 지정 + 사용자 삭제
    resp = c.post("/console/users/", {"action": "save", "user_id": "hong@company.com",
                  "name": "홍길동", "org_node_id": [str(pid), str(gid)]})
    assert resp.status_code == 302
    from app.db.repositories import UserRepository
    s_u = bridge.open_session()
    try:
        repo_u = UserRepository(s_u)
        u = repo_u.get_user("hong@company.com")
        assert u.display_name == "홍길동", "사용자 수정 반영 안 됨"
        assert set(repo_u.member_nodes("hong@company.com")) == {pid, gid}, "복수 소속 반영 안 됨"
    finally:
        s_u.close()
    # 조직도 관리에서 부서장(리더) 지정 — 사용자 관리 탭엔 역할 선택 없음
    resp = c.post("/console/org/", {"action": "set_leader", "node_id": str(pid),
                                    "leader_id": "hong@company.com"})
    assert resp.status_code == 302
    s_l = bridge.open_session()
    try:
        assert OrgRepository(s_l).nodes_led_by("hong@company.com") == [pid], "부서장 지정 안 됨"
    finally:
        s_l.close()
    # 삭제 대상 임시 사용자 생성 후 삭제
    c.post("/console/users/", {"action": "save", "user_id": "temp@company.com",
                               "org_node_id": [str(pid)]})
    resp = c.post("/console/users/", {"action": "delete", "user_id": "temp@company.com"})
    assert resp.status_code == 302
    s_u2 = bridge.open_session()
    try:
        assert UserRepository(s_u2).get_user("temp@company.com") is None, "사용자 삭제 안 됨"
    finally:
        s_u2.close()
    assert not DjUser.objects.filter(username="temp@company.com").exists()
    print("[사용자 ] 복수 소속 반영 · 조직도에서 부서장 지정 · 사용자+로그인계정 삭제 ✅")

    # 11) 업로드 → 업로더 검토 → 등록 확정(색인). 검토 전엔 검색에 안 나온다.
    from django.core.files.uploadedfile import SimpleUploadedFile
    from app.db.repositories import DocumentRepository
    c.force_login(staffer)
    up = SimpleUploadedFile("팀회식_공지.txt",
                            "회식 공지. 이번 주 금요일 저녁 회식이 있습니다.".encode("utf-8"),
                            content_type="text/plain")
    resp = c.post("/console/docs/", {"file": up})
    assert resp.status_code == 302 and "/console/docs/?doc=" in resp["Location"]
    req_doc_id = resp["Location"].split("doc=")[1]
    s3 = bridge.open_session()
    try:
        assert DocumentRepository(s3).get_status(req_doc_id) == "pending_review", \
            "업로드 직후엔 검토 대기여야 함"
    finally:
        s3.close()
    # 업로더 화면에 검토 폼(등록 확정 버튼) 노출
    dpage = c.get(f"/console/docs/?doc={req_doc_id}").content.decode()
    assert "등록 확정" in dpage and "내 검토 대기" in dpage, "업로더 검토 폼이 없음"
    # 업로더가 내용 확인 후 등록 확정 → 색인
    assert c.post(f"/console/review/{req_doc_id}/submit",
                  {"title": "팀 회식 공지", "doc_type": "notice",
                   "summary": "회식 공지", "keywords": "회식",
                   "lifecycle_status": "active"}).status_code == 302
    s3b = bridge.open_session()
    try:
        assert DocumentRepository(s3b).get_status(req_doc_id) == "indexed", "등록 확정 후 색인 안 됨"
    finally:
        s3b.close()
    # 본인 파트 문서: 직접 수정 가능(제목 변경) — 요청 없이 반영
    assert c.post(f"/console/docs/{req_doc_id}/action",
                  {"action": "save", "title": "팀 회식 공지 v2",
                   "lifecycle_status": "active"}).status_code == 302
    s3c = bridge.open_session()
    try:
        assert DocumentRepository(s3c).get(req_doc_id).classification.title_normalized == "팀 회식 공지 v2", \
            "본인 파트 문서 직접 수정 안 됨"
    finally:
        s3c.close()
    # 삭제는 부서장·관리자만 → 파트원은 삭제 요청 버튼
    dpage = c.get(f"/console/docs/?doc={req_doc_id}").content.decode()
    assert "삭제 요청" in dpage, "파트원 화면에 삭제 요청 버튼이 없음"
    print("[등록   ] 업로더 검토 → 등록 확정 색인 · 본인 파트 직접 수정 · 삭제만 요청 ✅")

    # 11b) 부서장은 삭제 요청 없이도 자기 부서 문서를 직접 삭제할 수 있다.
    s_lead = bridge.open_session()
    try:
        node_of_doc = DocumentRepository(s_lead).get(req_doc_id).governance.author_node_id
        OrgRepository(s_lead).set_leader(node_of_doc, "hong@company.com")  # 문서 작성부서의 부서장
        s_lead.commit()
    finally:
        s_lead.close()
    dpage = c.get(f"/console/docs/?doc={req_doc_id}").content.decode()
    assert "영구 삭제" in dpage, "부서장 화면에 영구 삭제 버튼이 없음"
    assert c.post(f"/console/docs/{req_doc_id}/action", {"action": "delete"}).status_code == 302
    s4 = bridge.open_session()
    try:
        assert DocumentRepository(s4).get(req_doc_id) is None   # 부서장이 직접 삭제함
    finally:
        s4.close()
    print("[삭제   ] 부서장이 삭제 요청 없이 자기 부서 문서 직접 삭제 ✅")

    # 11c) 제목 기본값(파일명) + 작성부서=조직노드 + 이전 업로드 기본값 프리필
    c.force_login(admin)

    def _f(name, body):
        return SimpleUploadedFile(name, body.encode("utf-8"), content_type="text/plain")

    respA = c.post("/console/docs/", {"file": _f("복리후생 안내.txt", "복리후생 제도 안내 문서입니다.")})
    aid = respA["Location"].split("doc=")[1]
    s_a = bridge.open_session()
    try:
        assert (DocumentRepository(s_a).get(aid).classification.title_normalized or "").strip(), \
            "제목이 파일명 기본값으로 채워지지 않음"
    finally:
        s_a.close()
    # 검토 확정: 작성부서=인터뷰파트(pid), 권한=채용그룹 전체
    assert c.post(f"/console/review/{aid}/submit",
                  {"title": "복리후생 안내", "doc_type": "notice", "summary": "복리후생 안내",
                   "keywords": "복리후생", "lifecycle_status": "active",
                   "author_node_id": str(pid), "access": [f"node:{gid}"]}).status_code == 302
    s_a2 = bridge.open_session()
    try:
        da = DocumentRepository(s_a2).get(aid)
        assert da.governance.author_node_id == pid, "작성부서(조직노드) 저장 안 됨"
        assert da.governance.access_selections == [f"node:{gid}"], "권한 저장 안 됨"
    finally:
        s_a2.close()
    # 다음 업로드가 직전 작성부서·권한으로 프리필되는지
    respB = c.post("/console/docs/", {"file": _f("교육 지원 안내.txt", "교육비 지원 제도 안내입니다.")})
    bid = respB["Location"].split("doc=")[1]
    s_b = bridge.open_session()
    try:
        db = DocumentRepository(s_b).get(bid)
        assert db.governance.author_node_id == pid, "이전 업로드 작성부서 프리필 안 됨"
        assert db.governance.access_selections == [f"node:{gid}"], "이전 업로드 권한 프리필 안 됨"
    finally:
        s_b.close()
    print("[기본값 ] 제목=파일명 · 작성부서=조직노드 · 이전 업로드 부서·권한 프리필 ✅")

    # 11d) 다중 업로드 → 순차 검토: 첫 건 확정 시 다음 건으로 이동 + 2번째가 1번째 확정본 상속
    resp = c.post("/console/docs/", {"file": [_f("규정1.txt", "첫 번째 규정 내용."),
                                              _f("규정2.txt", "두 번째 규정 내용.")]})
    m1 = resp["Location"].split("doc=")[1]
    # 첫 건을 '인터뷰파트(pid)' 작성부서로 확정 → 다음 건 자동 이동
    r_next = c.post(f"/console/review/{m1}/submit",
                    {"title": "규정1", "doc_type": "notice", "summary": "규정1",
                     "keywords": "규정", "lifecycle_status": "archived",
                     "author_node_id": str(pid), "access": [f"node:{gid}"]})
    assert r_next.status_code == 302 and "doc=" in r_next["Location"], "다음 검토 문서로 이동 안 함"
    m2 = r_next["Location"].split("doc=")[1]
    assert m2 != m1, "순차 이동이 같은 문서를 가리킴"
    # 2번째 검토 폼이 1번째 '확정본' 작성부서(pid)를 기본 선택으로 상속하는지
    p2 = c.get(f"/console/docs/?doc={m2}").content.decode()
    assert f'value="{pid}" selected' in p2, "2번째 파일이 직전 확정본 작성부서를 상속하지 않음"
    print("[다중   ] 동시 업로드 · 순차 검토 자동 이동 · 직전 확정본 설정 상속 ✅")

    # 11e) 검토 취소(폐기) + 등록 단계 연관 문서 지정 + 어휘 변경 내구성
    respC = c.post("/console/docs/", {"file": _f("취소할문서.txt", "취소 테스트용 문서.")})
    cid = respC["Location"].split("doc=")[1]
    rvpage = c.get(f"/console/docs/?doc={cid}").content.decode()
    assert "취소(폐기)" in rvpage and "관련 문서" in rvpage, "검토 폼에 취소/관련문서 UI 없음"
    assert c.post(f"/console/review/{cid}/cancel", {}).status_code == 302
    s_c = bridge.open_session()
    try:
        assert DocumentRepository(s_c).get(cid) is None, "검토 취소 시 문서가 폐기되지 않음"
    finally:
        s_c.close()
    # 등록 단계에서 연관 문서 지정(최대 3개 중 1개) → 링크 생성
    respD = c.post("/console/docs/", {"file": _f("본문서.txt", "관련문서 지정 테스트.")})
    did2 = respD["Location"].split("doc=")[1]
    c.post(f"/console/review/{did2}/submit",
           {"title": "본문서", "doc_type": "report", "summary": "본문", "keywords": "본문",
            "lifecycle_status": "active", "related_pick": [aid]})
    s_d = bridge.open_session()
    try:
        from app.db.repositories import RelationRepository
        assert aid in {r["doc_id"] for r in RelationRepository(s_d).related_ids(did2)}, \
            "등록 시 지정한 연관 문서가 연결되지 않음"
    finally:
        s_d.close()
    print("[등록UX ] 검토 취소 폐기 · 등록 시 연관 문서 지정 ✅")

    # 11f) 어휘 변경 내구성: 예전 doc_type('policy' 등 현재 어휘 밖)도 로딩·표시 가능
    s_leg = bridge.open_session()
    try:
        from app.db.models import Document as _Doc
        legacy = DocumentRepository(s_leg).get(did2)
        row = s_leg.get(_Doc, did2)
        meta = dict(row.metadata_json)
        meta["classification"]["doc_type"] = "policy_legacy_removed"  # 현재 어휘에 없음
        row.metadata_json = meta
        row.doc_type = "policy_legacy_removed"
        s_leg.commit()
        # 로딩이 깨지지 않고 안전 대체(unknown)로 열려야 함
        reopened = DocumentRepository(s_leg).get(did2)
        assert reopened is not None and reopened.classification.doc_type.value == "unknown", \
            "어휘 밖 doc_type 로딩이 안전 대체되지 않음"
    finally:
        s_leg.close()
    # 상세 화면도 500 없이 열려야 함
    assert c.get(f"/console/docs/?doc={did2}").status_code == 200
    print("[내구성 ] 어휘 밖 doc_type 문서도 오류 없이 로딩·표시 ✅")

    # 11g) 제목 날짜 정규화 + 문서명 검색 API + 다운로드명=제목
    respT = c.post("/console/docs/", {"file": _f("인사평가결과_2025-07-28.txt", "인사평가 결과 요약.")})
    tid2 = respT["Location"].split("doc=")[1]
    s_t = bridge.open_session()
    try:
        title = DocumentRepository(s_t).get(tid2).classification.title_normalized
        assert title.startswith("(25-0728)") and "인사평가결과" in title, f"제목 날짜 정규화 실패: {title}"
    finally:
        s_t.close()
    c.post(f"/console/review/{tid2}/submit",
           {"title": title, "doc_type": "report", "summary": "요약", "keywords": "평가",
            "lifecycle_status": "active"})
    # 문서명 검색 API
    import json as _json
    from urllib.parse import quote as _quote
    sr = c.get("/console/docs/search?q=" + _quote("인사평가"))
    hits = _json.loads(sr.content)["results"]
    assert any(h["id"] == tid2 for h in hits), "문서명 검색에서 등록 문서를 찾지 못함"
    # 다운로드 파일명 = 제목
    dl = c.get(f"/docs/original/{tid2}")
    assert dl.status_code in (200, 404)
    if dl.status_code == 200:
        assert "25-0728" in dl.get("Content-Disposition", ""), "다운로드 파일명이 제목 기반이 아님"
    print("[제목/검색] 날짜 (YY-MMDD) 정규화 · 문서명 검색 API · 다운로드명=제목 ✅")

    # 11h) 올리다 만(검토대기) 문서가 재업로드를 막지 않음(stale 교체)
    c.force_login(admin)
    stale = SimpleUploadedFile("stale_test.txt", "재업로드 테스트 문서.".encode("utf-8"),
                               content_type="text/plain")
    r1 = c.post("/console/docs/", {"file": stale})
    old_id = r1["Location"].split("doc=")[1]   # 검토 대기(미확정) 상태로 남김
    stale2 = SimpleUploadedFile("stale_test.txt", "재업로드 테스트 문서.".encode("utf-8"),
                                content_type="text/plain")
    r2 = c.post("/console/docs/", {"file": stale2})
    assert r2.status_code == 302 and "doc=" in r2["Location"], "stale 문서가 재업로드를 막음"
    assert r2["Location"].split("doc=")[1] != old_id
    s_st = bridge.open_session()
    try:
        assert DocumentRepository(s_st).get(old_id) is None, "stale 문서가 폐기되지 않음"
    finally:
        s_st.close()
    print("[재업로드] 올리다 만 문서가 재업로드를 막지 않음(stale 교체) ✅")

    # 11i) 사람 검색 API(이름+아이디) + 목록 작성자/작성일 열
    import json as _j
    hs = _j.loads(c.get("/console/users/search?q=" + _quote("admin")).content)["results"]
    assert any(h["id"] == "admin@company.com" and h["sub"] == "admin@company.com" for h in hs), \
        "사람 검색에 이름+아이디가 안 나옴"
    lp = c.get("/console/docs/").content.decode()
    assert "작성자" in lp and "작성일" in lp, "목록에 작성자·작성일 열이 없음"
    print("[사람검색] 이름+아이디 검색 API · 목록 작성자·작성일 열 ✅")

    # 11j) 여러 파일 일괄 삭제
    f_a = SimpleUploadedFile("bulk_a.txt", "일괄삭제 A".encode("utf-8"), content_type="text/plain")
    f_b = SimpleUploadedFile("bulk_b.txt", "일괄삭제 B".encode("utf-8"), content_type="text/plain")
    ra = c.post("/console/docs/", {"file": f_a}); ida = ra["Location"].split("doc=")[1]
    c.post(f"/console/review/{ida}/submit", {"title": "일괄 A", "doc_type": "report",
           "summary": "A", "keywords": "a", "lifecycle_status": "archived"})
    rb = c.post("/console/docs/", {"file": f_b}); idb = rb["Location"].split("doc=")[1]
    c.post(f"/console/review/{idb}/submit", {"title": "일괄 B", "doc_type": "report",
           "summary": "B", "keywords": "b", "lifecycle_status": "archived"})
    assert c.post("/console/docs/bulk-delete", {"doc_ids": [ida, idb]}).status_code == 302
    s_bd = bridge.open_session()
    try:
        dr = DocumentRepository(s_bd)
        assert dr.get(ida) is None and dr.get(idb) is None, "일괄 삭제가 안 됨"
    finally:
        s_bd.close()
    print("[일괄삭제] 여러 문서 한 번에 삭제 ✅")

    # 11k) 표 데이터(엑셀 명단) → Tier1 카드 임베딩 + Tier2 DuckDB 적재 + 구조화 답변
    import openpyxl as _oxl
    import tempfile as _tf2
    import pathlib as _pl2
    from app.datasets.detect import DATA_TABLE_ROW_THRESHOLD as _THR
    xpath = _pl2.Path(_tf2.gettempdir()) / "직원명단.xlsx"
    _wb = _oxl.Workbook(); _ws = _wb.active
    _ws.append(["사번", "이름", "부서", "급여"])
    for _i in range(_THR + 30):
        _ws.append([f"E{_i:04d}", f"이름{_i}", "인사팀" if _i % 2 else "재무팀", 3000 + _i])
    _wb.save(str(xpath))
    with open(xpath, "rb") as _f:
        up_xlsx = SimpleUploadedFile(
            "직원명단.xlsx", _f.read(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    c.force_login(admin)
    rX = c.post("/console/docs/", {"file": up_xlsx})
    xid = rX["Location"].split("doc=")[1]
    assert c.post(f"/console/review/{xid}/submit",
                  {"title": "직원명단", "doc_type": "report", "summary": "직원 명단",
                   "keywords": "명단", "lifecycle_status": "archived"}).status_code == 302
    # Tier 2 카탈로그 + DuckDB 적재 확인
    s_ds = bridge.open_session()
    try:
        from app.db.repositories import DatasetRepository
        from app.datasets.store import DuckDBStore
        from app.datasets.query import maybe_answer_structured
        from app.search.access import UserContext
        ds = DatasetRepository(s_ds).by_doc(xid)
        assert len(ds) == 1 and ds[0].row_count == _THR + 30, "데이터셋 적재 실패"
        cols, rows = DuckDBStore().query(f'SELECT count(*) AS n FROM "{ds[0].table_name}"')
        assert rows[0][0] == _THR + 30, "DuckDB 행수 불일치"
        # 구조화(SQL) 답변: 오프라인 fake LLM = count(*)
        ctx = UserContext(user_id="admin@company.com", groups=frozenset())
        da = maybe_answer_structured(s_ds, bridge.get_chat_llm(), ctx, "총 몇 명?", [xid])
        assert da is not None and str(_THR + 30) in da["text"], "구조화 답변 실패"
    finally:
        s_ds.close()
    print(f"[표데이터] Tier1 카드 색인 + Tier2 DuckDB 적재({_THR + 30}행)·SQL 답변 ✅")

    # 12) 연관 자동 감지: 보고서 + 같은 어간 별첨 업로드 → 자동 연결
    s5 = bridge.open_session()
    try:
        svc2 = bridge.get_review_service(s5)
        import tempfile as _tf, pathlib as _pl
        d = _pl.Path(_tf.gettempdir())
        (d / "평가결과보고서.txt").write_text("2026 평가 결과 보고서. 별첨 급여표 참고.", encoding="utf-8")
        (d / "평가결과보고서_별첨1.txt").write_text("급여표. 등급별 지급액 정리.", encoding="utf-8")
        rep = svc2.start_ingestion(str(d / "평가결과보고서.txt"), ingested_by="smoke")
        att = svc2.start_ingestion(str(d / "평가결과보고서_별첨1.txt"), ingested_by="smoke")
        s5.commit()
        from app.db.repositories import RelationRepository
        rel_ids = {r["doc_id"] for r in RelationRepository(s5).related_ids(rep)}
        assert att in rel_ids, "파일명 어간 기반 자동 연관이 안 됨"
    finally:
        s5.close()
    print("[연관   ] 보고서↔별첨 파일명 어간 자동 연결 ✅")

    # 13) 유사 문서 업로드 → AI 관계 제안(개정판/연관/무관)
    s6 = bridge.open_session()
    try:
        svc3 = bridge.get_review_service(s6)
        d = _pl.Path(_tf.gettempdir())
        (d / "연차규정_v1.txt").write_text(
            "연차 휴가 규정. 1년 근속 시 15일의 연차를 부여한다. 미사용분은 수당 지급.", encoding="utf-8")
        (d / "연차규정_v2.txt").write_text(
            "연차 휴가 규정. 1년 근속 시 15일의 연차를 부여한다. 미사용분은 수당으로 지급함.", encoding="utf-8")
        v1 = svc3.start_ingestion(str(d / "연차규정_v1.txt"), ingested_by="smoke")
        svc3.submit_review(v1, governance=GovernanceBlock(),
                           lifecycle_overrides={"status": "active"})
        v2 = svc3.start_ingestion(str(d / "연차규정_v2.txt"), ingested_by="smoke")
        s6.commit()
        cands = svc3.get_review(v2).similar_candidates
        assert cands and any(c["doc_id"] == v1 for c in cands), "유사 문서 감지 실패"
        assert any(c.get("ai_relation") for c in cands), "AI 관계 제안 없음"
    finally:
        s6.close()
    # 검토 화면 렌더에 관계 선택 UI(라디오)가 나오는지 확인
    c.force_login(admin)
    rpage = c.get(f"/console/review/?doc={v2}").content.decode()
    assert "유사한 기존 문서" in rpage and 'name="sim__' in rpage, "관계 선택 UI 미노출"
    print("[유사관계] 유사 문서 감지 + AI 관계 제안 + 관계 선택 UI ✅")

    print("\n✅ Django 웹 스모크 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
