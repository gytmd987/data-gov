"""관리 콘솔(관리자 전용) — 문서 검토 / 문서 관리 / 사용자 관리."""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

from django.contrib import messages
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from web import bridge
from web.authz import admin_required

from app import system_config
from app.ingestion.intake import DuplicateError
from app.manage.lifecycle import sweep_expired
from app.review.service import ENUM_OPTIONS
from app.schemas.enums import DocStatus, PiiType, SensitivityLevel
from app.schemas.metadata import GovernanceBlock

_PAGE_SIZE = 50            # 문서 관리 목록 한 페이지 행 수(대량에서도 화면이 안 먹통)
_EXPIRE_SOON_DAYS = 30     # 만료 임박 알림 기준


# ── 문서 검토 ────────────────────────────────────────────────────────────────
@admin_required
def review(request):
    session = bridge.open_session()
    try:
        svc = bridge.get_review_service(session)

        if request.method == "POST" and request.FILES.get("file"):
            f = request.FILES["file"]
            dest = Path(tempfile.gettempdir()) / f.name
            with open(dest, "wb") as out:
                for chunk in f.chunks():
                    out.write(chunk)
            try:
                svc.start_ingestion(str(dest), ingested_by=request.user.username)
                messages.success(request, f"'{f.name}' 업로드 완료 — AI 자동 채움 후 검토 대기에 추가됨")
            except DuplicateError:
                messages.warning(request, f"'{f.name}' 은 이미 등록된 문서입니다(내용 동일).")
            return redirect("console_review")

        pending = svc.list_pending()
        doc_id = request.GET.get("doc") or (pending[0]["doc_id"] if pending else None)
        view = svc.get_review(doc_id) if doc_id else None
        return render(request, "console/review.html", {
            "pending": pending, "view": view, "opts": ENUM_OPTIONS,
            "departments": system_config.departments(),
        })
    finally:
        session.close()


@admin_required
@require_POST
def review_submit(request, doc_id: str):
    session = bridge.open_session()
    try:
        svc = bridge.get_review_service(session)
        p = request.POST
        gov = GovernanceBlock(
            sensitivity_level=SensitivityLevel(p["sensitivity"]) if p.get("sensitivity") else None,
            contains_pii=None if p.get("pii") not in ("yes", "no") else p["pii"] == "yes",
            pii_types=[PiiType(x) for x in p.getlist("pii_types")],
            access_groups=p.getlist("groups"),
            owner=p.get("owner") or None,
        )
        cls = {"doc_type": p.get("doc_type"), "language": p.get("language"),
               "title_normalized": p.get("title") or None,
               "summary": p.get("summary") or None,
               "department": p.get("department") or None,
               "topics": [t.strip() for t in (p.get("topics") or "").split(",") if t.strip()]}
        life = {"status": p.get("lifecycle_status"),
                "effective_date": p.get("effective_date") or None,
                "expiry_date": p.get("expiry_date") or None}
        result = svc.submit_review(doc_id, governance=gov,
                                   classification_overrides=cls,
                                   lifecycle_overrides=life)
        if result.ok:
            old = p.get("supersede_old")
            if old:
                bridge.get_document_manager(session).supersede(old, doc_id)
                messages.info(request, "선택한 옛 버전을 검색에서 제외했습니다.")
            messages.success(request, "✅ 검증 통과 → 색인 완료. 검색에 노출됩니다.")
        else:
            why = ", ".join(result.missing_fields + result.errors)
            messages.error(request, f"⛔ 적재 차단(BLOCKED): {why}")
        return redirect("console_review")
    finally:
        session.close()


# ── 문서 관리 ────────────────────────────────────────────────────────────────
def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@admin_required
def docs(request):
    session = bridge.open_session()
    try:
        mgr = bridge.get_document_manager(session)
        q = request.GET.get("q") or None
        status = request.GET.get("status") or None
        doc_type = request.GET.get("doc_type") or None
        page = max(1, _int(request.GET.get("page"), 1))
        offset = (page - 1) * _PAGE_SIZE

        total = mgr.count_documents(text=q, lifecycle_status=status, doc_type=doc_type)
        doc_list = mgr.list_documents(text=q, lifecycle_status=status, doc_type=doc_type,
                                      limit=_PAGE_SIZE, offset=offset)
        num_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

        sel = request.GET.get("doc")
        doc = mgr.get(sel) if sel else None
        # 개정판 연결 후보: 선택 문서와 같은 유형 최근 200건(대량에서도 안전).
        supersede_candidates = []
        if doc is not None:
            supersede_candidates = [
                d for d in mgr.list_documents(
                    doc_type=doc.classification.doc_type.value, limit=200)
                if d["doc_id"] != sel]

        from app.db.repositories import UserRepository
        known = set(system_config.access_groups()) | \
            set(UserRepository(session).known_access_groups())

        today = date.today()
        expiring_soon = mgr.repo.count_expiring_soon(
            today, today + timedelta(days=_EXPIRE_SOON_DAYS))

        # 필터 유지용 쿼리스트링(페이징 링크에 재사용)
        qs = {k: v for k, v in (("q", q), ("status", status),
                                ("doc_type", doc_type)) if v}

        return render(request, "console/docs.html", {
            "docs": doc_list, "sel": sel, "doc": doc,
            "supersede_candidates": supersede_candidates,
            "q": q or "", "status_sel": status or "", "doc_type_sel": doc_type or "",
            "opts": ENUM_OPTIONS, "known_groups": sorted(known),
            "statuses": [s.value for s in DocStatus], "doc_types": ENUM_OPTIONS["doc_type"],
            "total": total, "page": page, "num_pages": num_pages,
            "page_start": offset + 1 if total else 0,
            "page_end": min(offset + _PAGE_SIZE, total),
            "expiring_soon": expiring_soon, "expire_days": _EXPIRE_SOON_DAYS,
            "filter_qs": qs,
        })
    finally:
        session.close()


@admin_required
@require_POST
def docs_bulk(request):
    """여러 문서를 한 번에 상태 변경(일괄 보관/만료/활성)."""
    session = bridge.open_session()
    try:
        mgr = bridge.get_document_manager(session)
        doc_ids = request.POST.getlist("doc_ids")
        action = request.POST.get("bulk_action")
        valid = {s.value for s in DocStatus}
        if not doc_ids:
            messages.warning(request, "선택된 문서가 없습니다.")
        elif action not in valid:
            messages.error(request, "잘못된 일괄 작업입니다.")
        else:
            for doc_id in doc_ids:
                mgr.set_status(doc_id, DocStatus(action))
            label = system_config.label(action)
            messages.success(request, f"{len(doc_ids)}건을 '{label}' 상태로 변경했습니다.")
        return redirect(request.POST.get("next") or "console_docs")
    finally:
        session.close()


@admin_required
@require_POST
def docs_sweep(request):
    """만료일이 지난 active 문서를 일괄 EXPIRED 처리(수동 실행)."""
    session = bridge.open_session()
    try:
        mgr = bridge.get_document_manager(session)
        expired = sweep_expired(mgr)
        if expired:
            messages.success(request, f"만료된 문서 {len(expired)}건을 정리했습니다.")
        else:
            messages.info(request, "만료 처리할 문서가 없습니다.")
        return redirect(request.POST.get("next") or "console_docs")
    finally:
        session.close()


@admin_required
@require_POST
def docs_action(request, doc_id: str):
    session = bridge.open_session()
    try:
        mgr = bridge.get_document_manager(session)
        action = request.POST.get("action")
        if action == "save":
            p = request.POST
            doc = mgr.get(doc_id)
            gov = doc.governance
            new_gov = GovernanceBlock(
                sensitivity_level=SensitivityLevel(p["sensitivity"]),
                contains_pii=gov.contains_pii, pii_types=gov.pii_types,
                access_groups=p.getlist("groups"),
                owner=p.get("owner") or None)
            mgr.update_metadata(doc_id, governance=new_gov,
                                lifecycle_overrides={"status": p.get("lifecycle_status")})
            messages.success(request, "저장 완료 — 검색 필터에 즉시 반영되었습니다.")
        elif action == "supersede":
            old = request.POST.get("old_id")
            if old:
                mgr.supersede(old, doc_id)
                messages.success(request, "옛 버전을 검색에서 제외했습니다.")
        elif action == "archive":
            mgr.archive(doc_id)
            messages.success(request, "보관 처리했습니다(검색 제외, 기록 유지).")
        elif action == "delete":
            mgr.delete(doc_id, hard=True)
            messages.warning(request, "영구 삭제했습니다.")
            return redirect("console_docs")
        return redirect(f"/console/docs/?doc={doc_id}")
    finally:
        session.close()


# ── 사용자 관리 ──────────────────────────────────────────────────────────────
@admin_required
def users(request):
    session = bridge.open_session()
    try:
        from app.db.repositories import UserRepository
        repo = UserRepository(session)

        if request.method == "POST":
            p = request.POST
            uid = (p.get("user_id") or "").strip()
            if uid:
                groups, clearance = repo.upsert_user_with_role(
                    uid, position=p["position"], job=p["job"],
                    display_name=p.get("name") or None)
                session.commit()
                # Django 로그인 계정도 함께 생성/갱신
                from django.contrib.auth.models import User as DjUser
                dj, created = DjUser.objects.get_or_create(
                    username=uid, defaults={"email": uid})
                if p.get("password"):
                    dj.set_password(p["password"])
                    dj.save()
                g_names = ", ".join(system_config.label(g) for g in sorted(groups))
                messages.success(request, f"'{uid}' 저장 → 그룹 [{g_names}] · 등급 {system_config.label(clearance)}"
                                 + (" (로그인 계정 생성됨)" if created else ""))
            return redirect("console_users")

        return render(request, "console/users.html", {
            "users": repo.list_users(),
            "positions": system_config.positions(),
            "jobs": system_config.jobs(),
        })
    finally:
        session.close()
