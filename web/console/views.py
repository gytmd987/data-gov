"""관리 콘솔(관리자 전용) — 문서 검토 / 문서 관리 / 사용자 관리."""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from web import bridge
from web.authz import (
    _email_of,
    admin_required,
    can_delete_doc,
    can_edit_doc,
    can_manage_doc,
    can_review_doc,
    is_admin,
    manage_scope,
)

from app import system_config
from app.ingestion.enrichment import ReadError
from app.ingestion.intake import DuplicateError
from app.manage.lifecycle import sweep_expired
from app.review.service import ENUM_OPTIONS
from app.schemas.enums import DocStatus
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import GovernanceBlock

_PAGE_SIZE = 50            # 문서 관리 목록 한 페이지 행 수(대량에서도 화면이 안 먹통)
_EXPIRE_SOON_DAYS = 30     # 만료 임박 알림 기준


def _related_docs(session, doc_id: str) -> list[dict]:
    """이 문서와 연관된 문서 [{doc_id, filename, reason, source}] (파일명 해석)."""
    from app.db.repositories import DocumentRepository, RelationRepository
    drepo = DocumentRepository(session)
    out = []
    for r in RelationRepository(session).related_ids(doc_id):
        d = drepo.get(r["doc_id"])
        if d is not None:
            out.append({"doc_id": r["doc_id"],
                        "filename": d.identification.source_filename,
                        "reason": r["reason"], "source": r["source"]})
    return out


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
            except ReadError as e:
                messages.error(request, f"⚠️ '{f.name}' {e}")
            return redirect("console_review")

        from app.db.repositories import DocumentRepository, OrgRepository
        from app.relations.classify import RELATION_LABELS
        pending = svc.list_pending()
        doc_id = request.GET.get("doc") or (pending[0]["doc_id"] if pending else None)
        view = svc.get_review(doc_id) if doc_id else None
        if view is not None:      # AI 관계 제안 한글 라벨 + 기본 선택값 부착
            for c in view.similar_candidates:
                rel = c.get("ai_relation") or "revision"
                c["ai_label"] = RELATION_LABELS.get(rel, rel)
                c["default_action"] = {"revision": "supersede", "related": "relate",
                                       "unrelated": "ignore"}.get(rel, "supersede")
        # 제목+형식 중복 후보(있으면 처리 방법 선택 배너 노출)
        dup = None
        if view is not None:
            d = svc.docs.get(view.doc_id)
            if d is not None:
                dup = DocumentRepository(session).find_active_by_title_format(
                    d.classification.title_normalized,
                    d.identification.file_format.value, exclude_doc_id=view.doc_id)
        return render(request, "console/review.html", {
            "pending": pending, "view": view, "opts": ENUM_OPTIONS,
            "departments": system_config.departments(),
            "node_opts": _org_options(OrgRepository(session)),
            "dup": dup, "admins": system_config.admin_emails(),
            "related": _related_docs(session, view.doc_id) if view else [],
        })
    finally:
        session.close()


@login_required
@require_POST
def review_submit(request, doc_id: str):
    session = bridge.open_session()
    try:
        svc = bridge.get_review_service(session)
        _doc = svc.docs.get(doc_id)
        if _doc is None:
            return redirect("console_docs")
        if not can_review_doc(session, request.user, _doc):
            messages.error(request, "이 문서를 검토·등록할 권한이 없습니다.")
            return redirect("console_docs")
        p = request.POST
        gov = GovernanceBlock(
            access_selections=p.getlist("access"),   # "node:N" / "head:N"; 빈 값=팀 전체
            author_id=p.get("author_id") or _email_of(request.user),
            author_name=p.get("author_name") or None,
            reporting_line=[x for x in p.getlist("reporting_line") if x],
        )
        cls = {"doc_type": p.get("doc_type"),
               "title_normalized": p.get("title") or None,
               "summary": p.get("summary") or None,
               "department": p.get("department") or None,
               "keywords": [k.strip() for k in (p.get("keywords") or "").split(",") if k.strip()],
               "related_parties": [x.strip() for x in (p.get("related_parties") or "").split(",") if x.strip()]}
        if p.get("language"):            # 비면 AI가 채운 값 유지
            cls["language"] = p.get("language")
        # expected_qa 는 AI가 채운 값을 유지(폼에서 덮어쓰지 않음)
        life = {"status": p.get("lifecycle_status"),
                "effective_date": p.get("effective_date") or None,
                "expiry_date": p.get("expiry_date") or None}

        # ── 제목+형식 중복 처리 ──────────────────────────────────────────────
        from app.db.repositories import DocumentRepository, RequestRepository
        drepo = DocumentRepository(session)
        existing = drepo.get(doc_id)
        fmt = existing.identification.file_format.value if existing else None
        title = cls["title_normalized"]
        dup = drepo.find_active_by_title_format(title, fmt, exclude_doc_id=doc_id) if fmt else None
        dup_action = p.get("dup_action")
        back = f"/console/docs/?doc={doc_id}"
        if dup is not None:
            if dup["doc_id"] in RequestRepository(session).pending_delete_doc_ids():
                messages.error(request, f"'{dup['filename']}' 삭제 승인 대기 중 — 같은 제목으로 등록할 수 없습니다.")
                return redirect(back)
            if dup_action == "request_delete":
                RequestRepository(session).create(
                    dup["doc_id"], "delete", _email_of(request.user),
                    doc_title=dup["filename"], author_node_id=dup.get("author_node_id"),
                    target_admin_id=p.get("target_admin") or None, note="중복 문서 정리 요청")
                session.commit()
                messages.info(request, "기존 문서 삭제를 요청했습니다. 승인 후 다시 등록하세요.")
                return redirect(back)
            if dup_action not in ("supersede", "proceed"):
                messages.error(request, f"제목+형식이 같은 문서가 있습니다: '{dup['filename']}'. 처리 방법을 선택하세요.")
                return redirect(back)

        result = svc.submit_review(doc_id, governance=gov,
                                   classification_overrides=cls,
                                   lifecycle_overrides=life)
        if result.ok:
            from app.db.repositories import RelationRepository
            mgr = bridge.get_document_manager(session)
            rel = RelationRepository(session)
            # 유사 문서 관계 확정(후보별: 새 버전 교체 / 연관 연결 / 무관)
            for key in p:
                if key.startswith("sim__"):
                    other = key[len("sim__"):]
                    act = p.get(key)
                    if act == "supersede":
                        mgr.supersede(other, doc_id)   # 기존을 이 문서의 이전 버전으로
                    elif act == "relate":
                        rel.link(doc_id, other, source="human", reason="검토 확정",
                                 created_by=_email_of(request.user))
            # 정확 중복(제목+형식) 처리에서 supersede 선택 시
            if dup is not None and dup_action == "supersede":
                mgr.supersede(dup["doc_id"], doc_id)
            session.commit()
            messages.success(request, "✅ 검증 통과 → 등록 완료. 검색에 노출됩니다.")
            return redirect(back)
        why = ", ".join(result.missing_fields + result.errors)
        messages.error(request, f"⛔ 적재 차단(BLOCKED): {why}")
        return redirect(back)
    finally:
        session.close()


# ── 문서 관리 ────────────────────────────────────────────────────────────────
def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@login_required
def docs(request):
    session = bridge.open_session()
    try:
        mgr = bridge.get_document_manager(session)
        me = _email_of(request.user)

        # 문서 등록(업로드) — AI 자동 채움 후 '내 검토 대기'에 올려 업로더가 확인·등록한다.
        if request.method == "POST" and request.FILES.get("file"):
            f = request.FILES["file"]
            dest = Path(tempfile.gettempdir()) / f.name
            with open(dest, "wb") as out:
                for chunk in f.chunks():
                    out.write(chunk)
            try:
                svc = bridge.get_review_service(session)
                new_id = svc.start_ingestion(str(dest), ingested_by=me)
                messages.success(request, f"'{f.name}' 업로드 완료 — AI가 채운 내용을 확인·수정한 뒤 등록을 확정하세요.")
                return redirect(f"/console/docs/?doc={new_id}")
            except DuplicateError:
                messages.warning(request, f"'{f.name}' 은 이미 등록된 문서입니다(내용 동일).")
            except ReadError as e:
                messages.error(request, f"⚠️ '{f.name}' {e}")
            return redirect("console_docs")

        q = request.GET.get("q") or None
        status = request.GET.get("status") or None
        doc_type = request.GET.get("doc_type") or None
        page = max(1, _int(request.GET.get("page"), 1))
        offset = (page - 1) * _PAGE_SIZE

        # 접근 범위: 관리자=전체, 부서장=내 subtree, 파트원=내 소속 노드(요청만 가능)
        is_adm, scope = manage_scope(session, request.user)
        author_ids = None
        can_manage = is_adm
        if not is_adm:
            from app.db.repositories import UserRepository
            if scope:                       # 부서장
                author_ids, can_manage = scope, True
            else:                           # 파트원: 내 소속 부서 문서(요청만)
                mine = set(UserRepository(session).member_nodes(_email_of(request.user)))
                author_ids = mine or {-1}

        # 목록은 등록(색인) 완료 문서만. 검토 대기는 아래 '내 검토 대기'로 분리.
        total = mgr.count_documents(text=q, lifecycle_status=status, doc_type=doc_type,
                                    author_node_ids=author_ids, indexed_only=True)
        doc_list = mgr.list_documents(text=q, lifecycle_status=status, doc_type=doc_type,
                                      limit=_PAGE_SIZE, offset=offset,
                                      author_node_ids=author_ids, indexed_only=True)
        num_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

        # 내가 올린 검토 대기 문서(등록 전 — 업로더가 확인해야 함)
        svc = bridge.get_review_service(session)
        my_pending = svc.list_pending(author_id=me)

        sel = request.GET.get("doc")
        doc = mgr.get(sel) if sel else None
        sel_status = svc.docs.get_status(sel) if sel else None
        pending_states = {IngestionStatus.PENDING_REVIEW.value, IngestionStatus.BLOCKED.value}

        # 선택 문서가 '검토 대기'이고 검토 권한이 있으면 검토 폼을 띄운다.
        review_view = None
        dup = None
        if doc is not None and sel_status in pending_states and can_review_doc(
                session, request.user, doc):
            from app.db.repositories import DocumentRepository
            from app.relations.classify import RELATION_LABELS
            review_view = svc.get_review(sel)
            for c in review_view.similar_candidates:
                rel = c.get("ai_relation") or "revision"
                c["ai_label"] = RELATION_LABELS.get(rel, rel)
                c["default_action"] = {"revision": "supersede", "related": "relate",
                                       "unrelated": "ignore"}.get(rel, "supersede")
            dup = DocumentRepository(session).find_active_by_title_format(
                doc.classification.title_normalized,
                doc.identification.file_format.value, exclude_doc_id=sel)

        # 개정판 연결 후보: 선택 문서와 같은 유형 최근 200건(대량에서도 안전).
        supersede_candidates = []
        if doc is not None and review_view is None:
            supersede_candidates = [
                d for d in mgr.list_documents(
                    doc_type=doc.classification.doc_type.value, limit=200,
                    indexed_only=True)
                if d["doc_id"] != sel]

        from app.db.repositories import OrgRepository
        node_opts = _org_options(OrgRepository(session))
        if doc is not None:
            sels = set(doc.governance.access_selections)
            for n in node_opts:
                n["sel_node"] = f"node:{n['id']}" in sels
                n["sel_head"] = f"head:{n['id']}" in sels

        today = date.today()
        expiring_soon = mgr.repo.count_expiring_soon(
            today, today + timedelta(days=_EXPIRE_SOON_DAYS))

        # 필터 유지용 쿼리스트링(페이징 링크에 재사용)
        qs = {k: v for k, v in (("q", q), ("status", status),
                                ("doc_type", doc_type)) if v}

        # 선택 문서 권한: 수정(본인 파트 포함) / 삭제(부서장·관리자만)
        can_edit_sel = doc is not None and can_edit_doc(session, request.user, doc)
        can_delete_sel = doc is not None and can_delete_doc(session, request.user, doc)

        # 부서장·관리자: 내 범위의 처리 대기 요청을 이 화면에서 바로 승인/반려
        pending_requests = []
        if can_manage:
            from app.db.repositories import RequestRepository
            for r in RequestRepository(session).list_pending():
                if is_adm or (r.author_node_id in scope):
                    pending_requests.append({
                        "id": r.id, "doc_id": r.doc_id, "doc_title": r.doc_title,
                        "request_type": r.request_type, "requester_id": r.requester_id,
                        "note": r.note})

        return render(request, "console/docs.html", {
            "docs": doc_list, "sel": sel, "doc": doc,
            "my_pending": my_pending, "review_view": review_view, "dup": dup,
            "supersede_candidates": supersede_candidates,
            "q": q or "", "status_sel": status or "", "doc_type_sel": doc_type or "",
            "opts": ENUM_OPTIONS, "node_opts": node_opts,
            "statuses": [s.value for s in DocStatus], "doc_types": ENUM_OPTIONS["doc_type"],
            "departments": system_config.departments(),
            "total": total, "page": page, "num_pages": num_pages,
            "page_start": offset + 1 if total else 0,
            "page_end": min(offset + _PAGE_SIZE, total),
            "expiring_soon": expiring_soon, "expire_days": _EXPIRE_SOON_DAYS,
            "filter_qs": qs,
            "is_admin_user": is_adm, "can_manage": can_manage,
            "can_edit_sel": can_edit_sel, "can_delete_sel": can_delete_sel,
            "pending_requests": pending_requests,
            "admins": system_config.admin_emails(),
            "related": _related_docs(session, sel) if doc else [],
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


@login_required
@require_POST
def docs_action(request, doc_id: str):
    session = bridge.open_session()
    try:
        mgr = bridge.get_document_manager(session)
        doc = mgr.get(doc_id)
        if doc is None:
            return redirect("console_docs")
        action = request.POST.get("action")

        # 삭제는 부서장·관리자만. 그 외 수정 작업은 본인 파트 문서까지 허용.
        if action == "delete":
            if not can_delete_doc(session, request.user, doc):
                messages.error(request, "삭제는 부서장·관리자만 가능합니다. '삭제 요청'을 이용하세요.")
                return redirect(f"/console/docs/?doc={doc_id}")
            mgr.delete(doc_id, hard=True)
            messages.warning(request, "영구 삭제했습니다.")
            return redirect("console_docs")

        if not can_edit_doc(session, request.user, doc):
            messages.error(request, "직접 수정 권한이 없습니다. '수정 요청'을 이용하세요.")
            return redirect(f"/console/docs/?doc={doc_id}")

        if action == "save":
            p = request.POST
            gov = doc.governance
            new_gov = GovernanceBlock(
                access_selections=p.getlist("access"),
                author_id=gov.author_id, author_name=gov.author_name,
                author_node_id=gov.author_node_id, reporting_line=gov.reporting_line)
            cls = {"doc_type": p.get("doc_type") or doc.classification.doc_type.value,
                   "title_normalized": p.get("title") or None,
                   "summary": p.get("summary") or None,
                   "department": p.get("department") or None,
                   "keywords": [k.strip() for k in (p.get("keywords") or "").split(",") if k.strip()],
                   "related_parties": [x.strip() for x in (p.get("related_parties") or "").split(",") if x.strip()]}
            life = {"status": p.get("lifecycle_status"),
                    "effective_date": p.get("effective_date") or None,
                    "expiry_date": p.get("expiry_date") or None}
            mgr.update_metadata(doc_id, governance=new_gov,
                                classification_overrides=cls, lifecycle_overrides=life)
            messages.success(request, "저장 완료 — 검색에 즉시 반영되었습니다.")
        elif action == "supersede":
            old = request.POST.get("old_id")
            if old:
                mgr.supersede(old, doc_id)
                messages.success(request, "옛 버전을 검색에서 제외했습니다.")
        elif action == "archive":
            mgr.archive(doc_id)
            messages.success(request, "보관 처리했습니다(검색 제외, 기록 유지).")
        return redirect(f"/console/docs/?doc={doc_id}")
    finally:
        session.close()


@login_required
@require_POST
def docs_relate(request, doc_id: str):
    """연관 문서 수동 추가/제거(관리자·부서장). 자동 감지 오류를 사람이 바로잡는 용도."""
    session = bridge.open_session()
    try:
        from app.db.repositories import RelationRepository
        mgr = bridge.get_document_manager(session)
        doc = mgr.get(doc_id)
        if doc is None or not can_edit_doc(session, request.user, doc):
            return redirect(f"/console/docs/?doc={doc_id}")
        rel = RelationRepository(session)
        other = (request.POST.get("other_id") or "").strip()
        if request.POST.get("action") == "unlink" and other:
            rel.unlink(doc_id, other)
            messages.success(request, "연관을 해제했습니다.")
        elif request.POST.get("action") == "add" and other and mgr.get(other) is not None:
            rel.link(doc_id, other, source="human", reason="수동",
                     created_by=_email_of(request.user))
            messages.success(request, "연관 문서로 연결했습니다.")
        session.commit()
        return redirect(request.POST.get("next") or f"/console/docs/?doc={doc_id}")
    finally:
        session.close()


@login_required
@require_POST
def docs_request(request, doc_id: str):
    """파트원 등 직접 권한이 없는 사용자의 수정/삭제 요청 생성."""
    session = bridge.open_session()
    try:
        from app.db.repositories import RequestRepository
        mgr = bridge.get_document_manager(session)
        doc = mgr.get(doc_id)
        if doc is None:
            return redirect("console_docs")
        rtype = request.POST.get("request_type")
        if rtype not in ("edit", "delete"):
            messages.error(request, "잘못된 요청입니다.")
            return redirect(f"/console/docs/?doc={doc_id}")
        RequestRepository(session).create(
            doc_id=doc_id, request_type=rtype, requester_id=_email_of(request.user),
            doc_title=doc.classification.title_normalized or doc.identification.source_filename,
            author_node_id=doc.governance.author_node_id,
            target_admin_id=request.POST.get("target_admin") or None,
            note=request.POST.get("note") or None)
        session.commit()
        kind = "삭제" if rtype == "delete" else "수정"
        messages.success(request, f"{kind} 요청을 등록했습니다. 부서장·관리자 승인 후 반영됩니다.")
        return redirect(f"/console/docs/?doc={doc_id}")
    finally:
        session.close()


# ── 수정/삭제 요청 승인 큐 ───────────────────────────────────────────────────
@login_required
def requests_queue(request):
    session = bridge.open_session()
    try:
        from app.db.repositories import RequestRepository
        is_adm, scope = manage_scope(session, request.user)
        if not is_adm and not scope:
            messages.error(request, "요청 승인 권한이 없습니다(부서장·관리자 전용).")
            return redirect("console_docs")
        rows = []
        for r in RequestRepository(session).list_pending():
            if is_adm or (r.author_node_id in scope):
                rows.append({"id": r.id, "doc_id": r.doc_id, "doc_title": r.doc_title,
                             "request_type": r.request_type, "requester_id": r.requester_id,
                             "target_admin_id": r.target_admin_id, "note": r.note,
                             "created_at": r.created_at})
        return render(request, "console/requests.html", {"rows": rows})
    finally:
        session.close()


@login_required
@require_POST
def request_resolve(request, req_id: int):
    session = bridge.open_session()
    try:
        from app.db.repositories import RequestRepository
        rr = RequestRepository(session)
        req = rr.get(int(req_id))
        if req is None or req.status != "pending":
            return redirect("console_requests")
        is_adm, scope = manage_scope(session, request.user)
        if not is_adm and req.author_node_id not in scope:
            messages.error(request, "이 요청을 처리할 권한이 없습니다.")
            return redirect(request.POST.get("next") or "console_docs")
        decision = request.POST.get("decision")
        if decision == "approve":
            if req.request_type == "delete":
                bridge.get_document_manager(session).delete(req.doc_id, hard=True)
                messages.success(request, "삭제 요청 승인 — 문서를 영구 삭제했습니다.")
            else:
                messages.success(request, "수정 요청 승인 — 문서 화면에서 반영하세요.")
            rr.resolve(req.id, "approved", _email_of(request.user))
        else:
            rr.resolve(req.id, "rejected", _email_of(request.user))
            messages.info(request, "요청을 반려했습니다.")
        session.commit()
        return redirect(request.POST.get("next") or "console_docs")
    finally:
        session.close()


# ── 사용자 관리 ──────────────────────────────────────────────────────────────
@admin_required
def users(request):
    session = bridge.open_session()
    try:
        from app.db.repositories import OrgRepository, UserRepository
        repo = UserRepository(session)
        org = OrgRepository(session)

        if request.method == "POST":
            p = request.POST
            from django.contrib.auth.models import User as DjUser
            uid = (p.get("user_id") or "").strip()
            action = p.get("action", "save")

            if action == "delete" and uid:
                repo.delete_user(uid)
                session.commit()
                DjUser.objects.filter(username=uid).delete()   # 로그인 계정도 삭제
                messages.warning(request, f"'{uid}' 사용자를 삭제했습니다(로그인 계정 포함).")
            elif uid:
                node_ids = [int(x) for x in p.getlist("org_node_id") if x]
                # 이름은 값이 있을 때만 갱신(빈칸이면 기존 유지)
                repo.set_memberships(uid, node_ids, display_name=p.get("name") or None)
                session.commit()
                # Django 로그인 계정도 함께 생성/갱신
                dj, created = DjUser.objects.get_or_create(
                    username=uid, defaults={"email": uid})
                if p.get("password"):
                    dj.set_password(p["password"])
                    dj.save()
                names = ", ".join(org.get(n).name for n in node_ids if org.get(n)) or "미배정"
                verb = "생성" if created else "수정"
                messages.success(request, f"'{uid}' {verb} 완료 → 소속: {names}")
            return redirect("console_users")

        node_opts = _org_options(org)
        node_names = {n["id"]: n["name"] for n in node_opts}
        user_list = repo.list_users()
        for u in user_list:
            u["node_names"] = ", ".join(node_names.get(i, str(i)) for i in u["node_ids"]) or "—"
        # 수정 대상(?edit=uid) 미리 채우기
        edit_uid = request.GET.get("edit")
        edit_user = next((u for u in user_list if u["user_id"] == edit_uid), None)
        edit_node_ids = set(edit_user["node_ids"]) if edit_user else set()
        return render(request, "console/users.html", {
            "users": user_list, "node_opts": node_opts,
            "edit_user": edit_user, "edit_node_ids": edit_node_ids,
        })
    finally:
        session.close()


# ── 조직도 관리 ──────────────────────────────────────────────────────────────
def _org_options(org) -> list[dict]:
    """조직도를 트리 순서(깊이 포함)로 평탄화 — 드롭다운·표 들여쓰기용."""
    tree = org.load_tree()
    nodes = {n.id: n for n in org.list_nodes()}
    children: dict = {}
    roots = []
    for n in org.list_nodes():
        if n.parent_id and n.parent_id in nodes:
            children.setdefault(n.parent_id, []).append(n)
        else:
            roots.append(n)
    out: list[dict] = []

    def walk(node, depth):
        out.append({"id": node.id, "name": node.name, "node_type": node.node_type,
                    "parent_id": node.parent_id, "depth": depth,
                    "indent": "  " * depth})
        for c in children.get(node.id, []):
            walk(c, depth + 1)

    for r in roots:
        walk(r, 0)
    return out


@admin_required
def org_console(request):
    session = bridge.open_session()
    try:
        from app.db.repositories import OrgRepository, UserRepository
        from app.org.tree import NODE_TYPES
        repo = OrgRepository(session)

        if request.method == "POST":
            p = request.POST
            action = p.get("action")
            if action == "set_leader":
                repo.set_leader(int(p["node_id"]), p.get("leader_id") or None)
                session.commit()
                messages.success(request, "부서장을 지정했습니다.")
                return redirect("console_org")
            if action == "create":
                name = (p.get("name") or "").strip()
                node_type = p.get("node_type")
                parent_id = int(p["parent_id"]) if p.get("parent_id") else None
                if name and node_type in NODE_TYPES:
                    repo.create_node(name, node_type, parent_id)
                    session.commit()
                    messages.success(request, f"'{name}' 추가됨.")
                else:
                    messages.error(request, "이름·유형을 확인하세요.")
            elif action == "rename":
                name = (p.get("name") or "").strip()
                if name:
                    repo.rename_node(int(p["node_id"]), name)
                    session.commit()
                    messages.success(request, "이름을 변경했습니다.")
            elif action == "delete":
                repo.delete_node(int(p["node_id"]))
                session.commit()
                messages.warning(request, "노드를 삭제했습니다(하위 포함, 배정 사용자는 해제).")
            return redirect("console_org")

        all_users = UserRepository(session).list_users()
        rows = _org_options(repo)
        for r in rows:
            r["members"] = repo.members(r["id"])
            r["leader"] = repo.leader(r["id"])
        return render(request, "console/org.html", {
            "rows": rows, "node_opts": rows, "all_users": all_users,
            "node_types": [{"value": t, "label": system_config.label(t)}
                           for t in ["team", "group", "part"]],
        })
    finally:
        session.close()
