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

    # 8) 관리 콘솔: 필터·페이징·일괄작업·만료 정리
    c.force_login(admin)
    assert c.get("/console/docs/?status=active&doc_type=policy&page=1").status_code == 200
    resp = c.post("/console/docs/bulk", {"doc_ids": [doc_id], "bulk_action": "archived"})
    assert resp.status_code == 302
    resp = c.post("/console/docs/sweep", {})
    assert resp.status_code == 302
    print("[콘솔+  ] 필터·페이징·일괄 보관·만료 정리 동작 ✅")

    # 9) 과거 문서 포함 검색: 방금 보관한 문서도 include_past 로 검색됨
    c.force_login(staffer)
    resp = c.post("/chat/send", json.dumps({"text": "연차는 며칠인가요?",
                  "use_rag": True, "include_past": True}),
                  content_type="application/json")
    d3 = resp.json()
    assert resp.status_code == 200
    assert d3["sources"] and any(s.get("is_past") for s in d3["sources"]), \
        "과거 포함 검색에서 보관 문서(is_past)가 나와야 함"
    print(f"[과거검색] 보관 문서 포함 검색 · 과거 배지 {len(d3['sources'])}건 ✅")

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

    # 11) 파트원 문서 등록(즉시 색인) → 본인 파트 문서 직접 수정 가능 → 삭제만 요청
    from django.core.files.uploadedfile import SimpleUploadedFile
    c.force_login(staffer)
    up = SimpleUploadedFile("팀회식_공지.txt",
                            "회식 공지. 이번 주 금요일 저녁 회식이 있습니다.".encode("utf-8"),
                            content_type="text/plain")
    assert c.post("/console/docs/", {"file": up}).status_code == 302
    # 등록 즉시 색인(검토 대기 없음) — 상태 확인
    s3 = bridge.open_session()
    try:
        from app.db.repositories import DocumentRepository
        drepo = DocumentRepository(s3)
        mine = [d for d in drepo.list_documents()
                if d["filename"] == "팀회식_공지.txt"]
        assert mine, "파트원 업로드 문서를 찾지 못함"
        req_doc_id = mine[0]["doc_id"]
        assert drepo.get_status(req_doc_id) == "indexed", "등록 즉시 색인 안 됨"
    finally:
        s3.close()
    # 본인 파트 문서: 직접 수정 가능(제목 변경) — 요청 없이 반영
    assert c.post(f"/console/docs/{req_doc_id}/action",
                  {"action": "save", "title": "팀 회식 공지",
                   "lifecycle_status": "active"}).status_code == 302
    s3b = bridge.open_session()
    try:
        assert DocumentRepository(s3b).get(req_doc_id).classification.title_normalized == "팀 회식 공지", \
            "본인 파트 문서 직접 수정 안 됨"
    finally:
        s3b.close()
    # 삭제는 부서장·관리자만 → 파트원은 삭제 요청
    dpage = c.get(f"/console/docs/?doc={req_doc_id}").content.decode()
    assert "삭제 요청" in dpage, "파트원 화면에 삭제 요청 버튼이 없음"
    assert c.post(f"/console/docs/{req_doc_id}/request",
                  {"request_type": "delete", "note": "중복"}).status_code == 302
    print("[등록   ] 파트원 즉시 색인 · 본인 파트 직접 수정 · 삭제만 요청 ✅")

    # 관리자·부서장은 '문서' 탭에서 대기 요청을 바로 승인 → 삭제 실행
    c.force_login(admin)
    dpage = c.get("/console/docs/").content.decode()
    assert "처리 대기 요청" in dpage and "회식" in dpage, "문서 탭에 대기 요청이 안 보임"
    import re as _re
    m = _re.search(r"/console/requests/(\d+)/resolve", dpage)
    assert m, "요청 승인 링크 없음"
    assert c.post(f"/console/requests/{m.group(1)}/resolve",
                  {"decision": "approve", "next": "/console/docs/"}).status_code == 302
    s4 = bridge.open_session()
    try:
        assert DocumentRepository(s4).get(req_doc_id) is None   # 삭제됨
    finally:
        s4.close()
    print("[승인   ] 문서 탭에서 삭제 요청 승인 → 문서 영구 삭제 ✅")

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
