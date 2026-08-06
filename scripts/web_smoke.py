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
    # 업로드 시 고른 '폴더'가 작성부서·권한·저장경로를 정한다
    respB = c.post("/console/docs/", {"file": _f("교육 지원 안내.txt", "교육비 지원 제도 안내입니다."),
                                      "folder_node_id": str(pid)})
    bid = respB["Location"].split("doc=")[1]
    s_b = bridge.open_session()
    try:
        drepo_b = DocumentRepository(s_b)
        db = drepo_b.get(bid)
        assert db.governance.author_node_id == pid, "폴더 기준 작성부서 세팅 안 됨"
        assert db.governance.access_selections == [f"node:{pid}"], "폴더 기준 권한 세팅 안 됨"
        stored = drepo_b.get_original_path(bid) or ""
        assert "인터뷰파트" in stored, f"폴더 경로에 저장되지 않음: {stored}"
    finally:
        s_b.close()
    print("[폴더   ] 업로드 폴더가 작성부서·열람권한·저장경로를 결정 ✅")

    # 11d) 다중 업로드(폴더 지정) → 순차 검토: 첫 건 확정 시 다음 건으로 이동
    resp = c.post("/console/docs/", {"file": [_f("규정1.txt", "첫 번째 규정 내용."),
                                              _f("규정2.txt", "두 번째 규정 내용.")],
                                     "folder_node_id": str(pid)})
    m1 = resp["Location"].split("doc=")[1]
    r_next = c.post(f"/console/review/{m1}/submit",
                    {"title": "규정1", "doc_type": "notice", "summary": "규정1",
                     "keywords": "규정", "lifecycle_status": "archived",
                     "author_node_id": str(pid), "access": [f"node:{pid}"]})
    assert r_next.status_code == 302 and "doc=" in r_next["Location"], "다음 검토 문서로 이동 안 함"
    m2 = r_next["Location"].split("doc=")[1]
    assert m2 != m1, "순차 이동이 같은 문서를 가리킴"
    # 2번째 파일도 같은 폴더 기준으로 작성부서가 세팅돼 있어야 한다
    p2 = c.get(f"/console/docs/?doc={m2}").content.decode()
    assert f'value="{pid}" selected' in p2, "2번째 파일의 폴더(작성부서) 기본 선택이 없음"
    print("[다중   ] 폴더 지정 동시 업로드 · 순차 검토 자동 이동 ✅")

    # 11d-2) 문서 관리 폴더 트리/필터 + 조직도 이름변경 재배치 + 문서 있는 폴더 삭제 차단
    lp = c.get("/console/docs/").content.decode()
    assert 'class="folder-pane"' in lp and "docs-layout" in lp, "폴더 트리/3분할 레이아웃 없음"
    # 상위(팀) 폴더로 필터해도 하위(파트) 문서가 보인다
    lp_team = c.get(f"/console/docs/?folder={tid}").content.decode()
    assert "규정1" in lp_team, "상위 폴더 필터에 하위 폴더 문서가 안 보임"
    # 조직도 이름 변경 → 저장 경로 재배치(다운로드 정상)
    assert c.post("/console/org/", {"action": "rename", "node_id": str(pid),
                                    "name": "면접파트"}).status_code == 302
    s_rn = bridge.open_session()
    try:
        moved_path = DocumentRepository(s_rn).get_original_path(m1) or ""
        assert "면접파트" in moved_path, f"이름변경 후 경로 재배치 안 됨: {moved_path}"
    finally:
        s_rn.close()
    assert c.get(f"/docs/original/{m1}").status_code in (200, 404)
    # 문서가 있는 폴더는 삭제 차단
    assert c.post("/console/org/", {"action": "delete", "node_id": str(pid)}).status_code == 302
    s_dl = bridge.open_session()
    try:
        assert OrgRepository(s_dl).get(pid) is not None, "문서가 있는데도 폴더가 삭제됨"
    finally:
        s_dl.close()
    print("[폴더UI ] 폴더 트리·상위필터(하위포함) · 이름변경 재배치 · 문서 있는 폴더 삭제 차단 ✅")

    # 11d-3) 채팅 검색의 폴더 스코프: 상위 폴더 선택 시 하위 문서 포함, 다른 폴더는 제외
    s_fs = bridge.open_session()
    try:
        from app.schemas.metadata import doc_level_payload
        from app.search.access import AccessPolicy as _AP
        from app.search.access import UserContext as _UC
        pay = doc_level_payload(DocumentRepository(s_fs).get(m1))
        tree_fs = OrgRepository(s_fs).load_tree()
        u = _UC(user_id="admin@company.com", groups=frozenset({f"n:{pid}"}))
        assert _AP.for_user(u, folder_node_ids=frozenset(tree_fs.subtree(tid))).allows(pay), \
            "상위 폴더 스코프에 하위 문서가 안 잡힘"
        other = OrgRepository(s_fs).create_node("타그룹", "group", parent_id=tid)
        s_fs.commit()
        assert not _AP.for_user(u, folder_node_ids=frozenset(tree_fs.subtree(other.id))).allows(pay), \
            "다른 폴더 스코프인데 문서가 잡힘"
    finally:
        s_fs.close()
    cpage = c.get("/").content.decode()
    assert 'id="folderScope"' in cpage, "채팅에 검색 범위(폴더) 선택이 없음"
    print("[검색범위] 채팅 폴더 스코프(상위 선택 시 하위 포함) ✅")

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

    # 11j-2) 한국어 로케일 날짜로 저장해도 깨지지 않음 + 문서 정보에서 관련 문서 추가
    f_d = SimpleUploadedFile("날짜테스트.txt", "날짜 저장 테스트 문서.".encode("utf-8"),
                             content_type="text/plain")
    rd = c.post("/console/docs/", {"file": f_d, "folder_node_id": str(pid)})
    dtid = rd["Location"].split("doc=")[1]
    c.post(f"/console/review/{dtid}/submit", {"title": "날짜 테스트", "doc_type": "report",
           "summary": "날짜", "keywords": "날짜", "lifecycle_status": "archived",
           "author_node_id": str(pid)})
    # 화면에 표시되던 '2026년 6월 30일' 형식을 그대로 저장해도 성공해야 한다
    assert c.post(f"/console/docs/{dtid}/action",
                  {"action": "save", "title": "날짜 테스트", "lifecycle_status": "archived",
                   "effective_date": "2026년 6월 30일",
                   "expiry_date": "2027.01.02"}).status_code == 302
    s_dt = bridge.open_session()
    try:
        life = DocumentRepository(s_dt).get(dtid).lifecycle
        assert str(life.effective_date) == "2026-06-30", f"작성일 저장 실패: {life.effective_date}"
        assert str(life.expiry_date) == "2027-01-02", f"유효일 저장 실패: {life.expiry_date}"
    finally:
        s_dt.close()
    # 문서 정보에서 관련 문서 '추가'
    assert c.post(f"/console/docs/{dtid}/relate",
                  {"action": "add", "other_id": aid}).status_code == 302
    s_rel = bridge.open_session()
    try:
        from app.db.repositories import RelationRepository as _RR
        assert aid in {r["doc_id"] for r in _RR(s_rel).related_ids(dtid)}, "관련 문서 추가 실패"
    finally:
        s_rel.close()
    dpage2 = c.get(f"/console/docs/?doc={dtid}").content.decode()
    assert "2026-06-30" in dpage2, "상세에 ISO 날짜가 표시되지 않음"
    print("[날짜/관련] 한국어 날짜 표기 저장 정상 · 문서 정보에서 관련 문서 추가 ✅")

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
    # 등록 화면: 경고형 라디오는 없고, '버전 정리' 피커에 AI 후보가 제안된다
    c.force_login(admin)
    rpage = c.get(f"/console/docs/?doc={v2}").content.decode()
    assert 'name="sim__' not in rpage, "경고형 관계 선택 라디오가 아직 남아 있음"
    assert 'data-name="old_id"' in rpage, "등록 단계 버전 정리 피커가 없음"
    assert "이전 버전 후보" in rpage, "AI 이전 버전 후보 제안이 없음"
    assert rpage.count('data-name="related_pick"') == 1, "관련 문서는 검색 전용이어야 함"
    print("[유사관계] 유사 문서 감지 · 등록 단계 버전 정리(AI 후보+검색) · 관련문서 검색 전용 ✅")

    # 14) 권한: 형제 파트 가시성 + 자동완성/상세 URL 우회 차단
    s7 = bridge.open_session()
    try:
        from app.db.repositories import OrgRepository as _Org, UserRepository as _Users
        org7 = _Org(s7)
        m_grp = org7.create_node("ㅁ그룹", "group", parent_id=team.id)
        n_part = org7.create_node("ㄴ파트", "part", parent_id=m_grp.id)
        o_part = org7.create_node("ㅇ파트", "part", parent_id=m_grp.id)
        x_grp = org7.create_node("ㅅ그룹", "group", parent_id=team.id)
        u7 = _Users(s7)
        u7.set_memberships("nlee@company.com", [n_part.id], display_name="ㄴ파트원")
        u7.set_memberships("okim@company.com", [o_part.id], display_name="ㅇ파트원")
        u7.set_memberships("xpark@company.com", [x_grp.id], display_name="ㅅ그룹원")
        s7.commit()

        svc7 = bridge.get_review_service(s7)
        d = _pl.Path(_tf.gettempdir())
        (d / "그룹공개문서.txt").write_text("ㅁ그룹 전체에 공개하는 운영 안내.", encoding="utf-8")
        (d / "파트전용문서.txt").write_text("ㄴ파트 내부에서만 보는 대외비 메모.", encoding="utf-8")
        pub = svc7.start_ingestion(str(d / "그룹공개문서.txt"), ingested_by="nlee@company.com",
                                   folder_node_id=n_part.id)
        svc7.submit_review(pub, governance=GovernanceBlock(
            author_node_id=n_part.id, access_selections=[f"node:{m_grp.id}"]),
            lifecycle_overrides={"status": "active"})
        sec = svc7.start_ingestion(str(d / "파트전용문서.txt"), ingested_by="nlee@company.com",
                                   folder_node_id=n_part.id)
        svc7.submit_review(sec, governance=GovernanceBlock(
            author_node_id=n_part.id, access_selections=[f"node:{n_part.id}"]),
            lifecycle_overrides={"status": "active"})
        s7.commit()
    finally:
        s7.close()

    for uid in ("nlee@company.com", "okim@company.com", "xpark@company.com"):
        DjUser.objects.get_or_create(username=uid, defaults={"email": uid})

    # ㅇ파트 사람: 상위(ㅁ그룹) 공개 문서는 보이고, 형제 파트 전용 문서는 안 보인다
    c_o = Client()
    c_o.force_login(DjUser.objects.get(username="okim@company.com"))
    page = c_o.get("/console/docs/").content.decode()
    assert "그룹공개문서" in page, "상위 부서 공개 문서가 형제 파트에 안 보임"
    assert "파트전용문서" not in page, "형제 파트 전용 문서가 노출됨"

    # 자동완성(연관/버전 지정)이 권한을 우회하지 못한다
    hits = c_o.get("/console/docs/search?q=파트전용").json()["results"]
    assert hits == [], f"권한 없는 문서가 문서 검색에 노출됨: {hits}"
    assert c_o.get("/console/docs/search?q=그룹공개").json()["results"], "볼 수 있는 문서는 검색돼야 함"

    # 상세는 URL 로 직접 열어도 막힌다 + 원본 다운로드도 404
    detail = c_o.get(f"/console/docs/?doc={sec}", follow=True).content.decode()
    assert "파트전용문서" not in detail, "URL 직접 접근으로 상세가 열림"
    assert c_o.get(f"/docs/original/{sec}").status_code == 404, "권한 없는 원본이 내려받아짐"

    # 다른 그룹 사람에겐 둘 다 안 보인다
    c_x = Client()
    c_x.force_login(DjUser.objects.get(username="xpark@company.com"))
    page_x = c_x.get("/console/docs/").content.decode()
    assert "그룹공개문서" not in page_x and "파트전용문서" not in page_x, "타 그룹에 문서가 노출됨"
    print("[권한   ] 상위 부서 공개=형제 파트 노출 · 미권한 문서는 목록/검색/상세/다운로드 전부 차단 ✅")

    # 15) 비밀번호 변경(로그인 사용자 본인)
    pw_user, _ = DjUser.objects.get_or_create(username="pw@company.com",
                                              defaults={"email": "pw@company.com"})
    pw_user.set_password("OldPass!2026")
    pw_user.save()
    c_pw = Client()
    assert c_pw.login(username="pw@company.com", password="OldPass!2026")
    assert c_pw.get("/accounts/password/").status_code == 200
    r_pw = c_pw.post("/accounts/password/", {"old_password": "OldPass!2026",
                                             "new_password1": "BrandNew!2026",
                                             "new_password2": "BrandNew!2026"})
    assert r_pw.status_code == 302, "비밀번호 변경 실패"
    assert Client().login(username="pw@company.com", password="BrandNew!2026"), "새 비밀번호로 로그인 불가"
    print("[비밀번호] 본인 비밀번호 변경 · 새 비밀번호 로그인 ✅")

    # 16) 목록 정렬 + 일괄 변경(권한/폴더/상태/종류) + 보고선 체크박스
    c.force_login(admin)
    for key in ("title", "effective", "created", "doc_type", "status"):
        assert c.get(f"/console/docs/?sort={key}&dir=asc").status_code == 200, f"정렬 {key} 실패"
    page = c.get("/console/docs/?sort=title&dir=asc").content.decode()
    assert "제목순" in page and "오름차순" in page, "정렬 UI가 없음"

    s8 = bridge.open_session()
    try:
        from app.db.repositories import DocumentRepository as _DR, OrgRepository as _OR2
        org8 = _OR2(s8)
        target = org8.create_node("보안그룹", "group", parent_id=team.id)
        s8.commit()
        target_id = target.id
        ids = [d["doc_id"] for d in _DR(s8).list_documents(indexed_only=True)][:2]
        assert ids, "일괄 변경 대상 문서가 없음"
    finally:
        s8.close()

    # 열람 권한 일괄 적용 → 고른 부서 토큰으로 덮어써진다
    r_bulk = c.post("/console/docs/bulk-update",
                    {"doc_ids": ids, "field": "access", "access": [f"node:{target_id}"],
                     "next": "/console/docs/"})
    assert r_bulk.status_code == 302, "일괄 권한 변경 실패"
    s9 = bridge.open_session()
    try:
        from app.db.repositories import DocumentRepository as _DR3
        for doc_id in ids:
            gov = _DR3(s9).get(doc_id).governance
            assert gov.access_selections == [f"node:{target_id}"], "권한이 안 바뀜"
            assert f"n:{target_id}" in gov.access_tokens, "열람 토큰이 확장되지 않음"
    finally:
        s9.close()

    # 상태 일괄 변경
    c.post("/console/docs/bulk-update", {"doc_ids": ids[:1], "field": "status",
                                         "lifecycle_status": "expired",
                                         "next": "/console/docs/"})
    s10 = bridge.open_session()
    try:
        from app.db.repositories import DocumentRepository as _DR4
        assert _DR4(s10).get(ids[0]).lifecycle.status.value == "expired", "상태가 안 바뀜"
    finally:
        s10.close()
    print("[문서관리] 정렬(제목·작성일·종류…) · 열람권한/상태 일괄 변경 ✅")

    # 보고선: 자율 기재가 아니라 설정된 항목 체크박스
    from app import system_config as _sc
    lines = _sc.reporting_lines()
    assert lines, "보고선 후보가 설정에 없음"
    detail = c.get(f"/console/docs/?doc={ids[0]}").content.decode()
    assert f'name="reporting_line" value="{lines[0]}"' in detail, "보고선 체크박스가 없음"
    c.post(f"/console/docs/{ids[0]}/action",
           {"action": "save", "doc_type": "report", "title": "보고선 테스트",
            "lifecycle_status": "active", "reporting_line": [lines[0], lines[-1]]})
    s11 = bridge.open_session()
    try:
        from app.db.repositories import DocumentRepository as _DR5
        got = _DR5(s11).get(ids[0]).governance.reporting_line
        assert got == [lines[0], lines[-1]], f"보고선 저장 실패: {got}"
    finally:
        s11.close()
    print("[보고선 ] 관리자 설정 항목 체크박스로 선택·저장 ✅")

    # 17) 하위 폴더: 파트원이 만들고 · 기본 권한이 업로드에 채워지고 · 서버 폴더 동기화
    c_n = Client()
    c_n.force_login(DjUser.objects.get(username="nlee@company.com"))
    r_mk = c_n.post("/console/folders/", {"action": "create", "name": "대외공개",
                                          "node_id": n_part.id,
                                          "next": "/console/docs/"})
    assert r_mk.status_code == 302, "파트원이 하위 폴더를 못 만듦"

    s12 = bridge.open_session()
    try:
        from app.db.repositories import OrgRepository as _OR3
        org12 = _OR3(s12)
        new_folder = next(n for n in org12.list_nodes() if n.name == "대외공개")
        assert new_folder.node_type == "folder" and new_folder.parent_id == n_part.id
        folder_id = new_folder.id
        # 디스크에도 만들어졌는지
        from app.manage.storage import fs_dir
        assert fs_dir(org12.load_tree(), folder_id).is_dir(), "폴더가 디스크에 안 생김"
    finally:
        s12.close()

    # 폴더 기본 권한 = 팀 전체(상위) → 이 폴더로 올리면 자동으로 그 권한이 채워진다
    c_n.post("/console/folders/", {"action": "default_access", "node_id": folder_id,
                                   "access": [f"node:{team.id}"],
                                   "next": "/console/docs/"})
    s13 = bridge.open_session()
    try:
        svc13 = bridge.get_review_service(s13)
        d = _pl.Path(_tf.gettempdir())
        (d / "대외안내문.txt").write_text("대외 공개용 채용 안내문입니다.", encoding="utf-8")
        did = svc13.start_ingestion(str(d / "대외안내문.txt"),
                                    ingested_by="nlee@company.com",
                                    folder_node_id=folder_id)
        s13.commit()
        gov = svc13.docs.get(did).governance
        assert gov.author_node_id == folder_id, "저장 위치가 폴더가 아님"
        assert gov.access_selections == [f"node:{team.id}"], \
            f"폴더 기본 권한이 안 채워짐: {gov.access_selections}"
    finally:
        s13.close()

    # 권한 드롭다운에는 폴더가 나오면 안 된다(권한은 부서 단위로만)
    page_f = c_n.get(f"/console/docs/?folder={folder_id}").content.decode()
    assert f'name="access" value="node:{folder_id}"' not in page_f, \
        "열람 권한 선택지에 폴더가 노출됨"
    assert "대외공개" in page_f, "폴더 트리에 새 폴더가 안 보임"

    # 서버에서 직접 만든 폴더 → 관리자 동기화로 화면에 등록
    s14 = bridge.open_session()
    try:
        from app.db.repositories import OrgRepository as _OR4
        from app.manage.storage import fs_dir as _fd
        (_fd(_OR4(s14).load_tree(), n_part.id) / "서버생성폴더").mkdir(parents=True,
                                                                exist_ok=True)
    finally:
        s14.close()
    assert c_n.post("/console/folders/", {"action": "sync"}).status_code == 302
    s15 = bridge.open_session()
    try:
        from app.db.repositories import OrgRepository as _OR5
        assert not any(n.name == "서버생성폴더" for n in _OR5(s15).list_nodes()), \
            "관리자가 아닌데 동기화가 실행됨"
    finally:
        s15.close()
    c.force_login(admin)
    assert c.post("/console/folders/", {"action": "sync"}).status_code == 302
    s16 = bridge.open_session()
    try:
        from app.db.repositories import OrgRepository as _OR6
        node = next((n for n in _OR6(s16).list_nodes() if n.name == "서버생성폴더"), None)
        assert node is not None and node.parent_id == n_part.id, "서버 폴더가 등록 안 됨"
    finally:
        s16.close()
    print("[하위폴더] 파트원 폴더 생성 · 폴더 기본권한 자동 채움 · 관리자 전용 서버 동기화 ✅")

    # 18) 화면에 내부 정보가 새지 않는다(템플릿 주석·내부 필드명·원시 상태값)
    leaked = []
    for url in ("/console/docs/", "/console/org/", "/console/users/", "/"):
        html = c.get(url).content.decode()
        for token in ("{#", "#}", "{%", "title_normalized", "doc_type ", "auto_filled",
                      "pending_review", "PENDING_REVIEW", "Traceback"):
            if token in html:
                leaked.append(f"{url}: {token}")
    assert not leaked, f"화면에 내부 정보 노출: {leaked}"
    print("[화면정리] 템플릿 주석·내부 필드명·원시 상태값 미노출 ✅")

    print("\n✅ Django 웹 스모크 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
