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

    # 7) 일반 사용자 문서 등록 페이지 접근(관리자 아님)
    assert c.get("/submit/").status_code == 200
    print("[등록   ] 일반 사용자 문서 등록 페이지 접근 ✅")

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
    # 사용자 관리 화면에서 노드·역할 배정
    resp = c.post("/console/users/", {"user_id": "hong@company.com", "name": "홍파트원",
                  "org_node_id": str(pid), "org_role": "파트원"})
    assert resp.status_code == 302
    body = c.get("/console/org/").content.decode()
    assert "People팀" in body and "인터뷰파트" in body and "홍파트원" in body
    print("[조직도 ] 팀>그룹>파트 생성 · 사용자 배정 · 계층 표시 ✅")

    print("\n✅ Django 웹 스모크 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
