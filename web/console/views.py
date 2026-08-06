"""관리 콘솔(관리자 전용) — 문서 검토 / 문서 관리 / 사용자 관리."""

from __future__ import annotations

import tempfile
from datetime import date, timedelta
from pathlib import Path

from django.conf import settings as django_settings
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
    can_read_doc,
    can_manage_folder,
    can_review_doc,
    is_admin,
    manage_scope,
    visibility_for,
)

from app import system_config
from app.ingestion.enrichment import ReadError
from app.ingestion.intake import DuplicateError
from app.manage.lifecycle import sweep_expired
from app.review.service import ENUM_OPTIONS, USER_DOC_STATUSES
from app.schemas.ingestion import IngestionStatus
from app.schemas.metadata import GovernanceBlock

_PAGE_SIZE = 50            # 문서 관리 목록 한 페이지 행 수(대량에서도 화면이 안 먹통)
_EXPIRE_SOON_DAYS = 30     # 만료 임박 알림 기준


def _to_iso(value):
    """폼 날짜 입력 → 'YYYY-MM-DD'. '2026년 6월 30일' 같은 표기도 받아준다.

    (화면이 한국어 로케일로 날짜를 렌더링해도 저장이 깨지지 않도록 방어.)
    """
    from app.ingestion.titletools import to_iso
    return to_iso(value)


def _related_docs(session, doc_id: str, visible_to=None) -> list[dict]:
    """이 문서와 연관된 문서 [{doc_id, filename, reason, source}] (파일명 해석).

    visible_to 를 주면 **열람 권한이 있는 연관 문서만** 보여준다(연결을 타고 남의
    부서 문서 제목이 새어 나가지 않도록).
    """
    from app.db.repositories import DocumentRepository, RelationRepository
    drepo = DocumentRepository(session)
    links = RelationRepository(session).related_ids(doc_id)
    allowed = drepo.readable_doc_ids([r["doc_id"] for r in links], visible_to)
    out = []
    for r in links:
        if r["doc_id"] not in allowed:
            continue
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
            except DuplicateError as e:
                messages.warning(request, f"'{f.name}' 은 이미 등록된 문서입니다({e.reason}).")
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
            "reporting_lines": system_config.reporting_lines(),
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
        # AI가 종류를 판단 못 했으면 사람이 반드시 고르게 한다(빈 값으로 등록 금지).
        if p.get("doc_type") not in ENUM_OPTIONS["doc_type"]:
            messages.error(request, "문서 종류를 골라주세요.")
            return redirect(f"/console/docs/?doc={doc_id}")
        node_id, dept_name = _dept_from_form(session, p)
        if node_id is None:              # 작성부서 미선택 → 적재 시 기본값 유지
            node_id, dept_name = _doc.governance.author_node_id, _doc.classification.department
        gov = GovernanceBlock(
            access_selections=p.getlist("access"),   # "node:N" / "head:N"; 빈 값=팀 전체
            author_id=p.get("author_id") or _email_of(request.user),
            author_name=p.get("author_name") or None,
            author_node_id=node_id,                  # 작성부서 = 조직도 노드(관리 부서)
            reporting_line=[x for x in p.getlist("reporting_line") if x],
        )
        cls = {"doc_type": p.get("doc_type"),
               "title_normalized": p.get("title") or None,
               "summary": p.get("summary") or None,
               "department": dept_name,              # 노드 이름(표시·검색용)
               "keywords": [k.strip() for k in (p.get("keywords") or "").split(",") if k.strip()],
               "related_parties": [x.strip() for x in (p.get("related_parties") or "").split(",") if x.strip()]}
        if p.get("language"):            # 비면 AI가 채운 값 유지
            cls["language"] = p.get("language")
        # expected_qa 는 AI가 채운 값을 유지(폼에서 덮어쓰지 않음)
        life = {"status": p.get("lifecycle_status"),
                "effective_date": _to_iso(p.get("effective_date")),
                "expiry_date": _to_iso(p.get("expiry_date"))}

        # ── 제목+형식 중복 처리 ──────────────────────────────────────────────
        from app.db.repositories import DocumentRepository, RequestRepository
        drepo = DocumentRepository(session)
        vis = visibility_for(session, request.user)
        existing = drepo.get(doc_id)
        fmt = existing.identification.file_format.value if existing else None
        title = cls["title_normalized"]
        dup = _visible_dup(session, drepo, vis, title, fmt, exclude=doc_id)
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
            # 등록 단계 '버전 정리': 지정한 옛 문서를 이 문서의 이전 버전으로 처리.
            # 폼 값은 조작될 수 있으므로 볼 수 있는 문서인지 서버에서 다시 확인한다.
            old_id = (p.get("old_id") or "").strip()
            if (old_id and old_id != doc_id
                    and drepo.readable_doc_ids([old_id], vis)):
                mgr.supersede(old_id, doc_id)
            # 정확 중복(제목+형식) 처리에서 supersede 선택 시
            if dup is not None and dup_action == "supersede":
                mgr.supersede(dup["doc_id"], doc_id)
            # 등록 단계에서 수동 지정한 연관 문서(최대 3개)
            picked = [x for x in dict.fromkeys(p.getlist("related_pick")) if x][:3]
            for other in drepo.readable_doc_ids(picked, vis):
                rel.link(doc_id, other, source="human", reason="등록 시 지정",
                         created_by=_email_of(request.user))
            svc.finalize_original_name(doc_id)   # 서버 원본 파일명을 제목으로
            # 표 데이터(엑셀 명단 등)면 DuckDB 에 구조화 적재(Tier 2)
            try:
                from app.datasets.loader import ingest_if_tabular
                ingest_if_tabular(session, svc.docs.get(doc_id))
            except Exception:
                pass   # 구조화 적재 실패해도 문서 등록 자체는 유지
            session.commit()
            # 여러 건을 올렸으면 남은 검토 대기 문서로 자동 이동(순차 검토)
            remaining = [d for d in svc.list_pending(author_id=_email_of(request.user))
                         if d["doc_id"] != doc_id]
            if remaining:
                messages.success(request, f"✅ 등록 완료. 남은 검토 대기 {len(remaining)}건 — 다음 문서를 검토하세요.")
                return redirect(f"/console/docs/?doc={remaining[0]['doc_id']}")
            messages.success(request, "✅ 검증 통과 → 등록 완료. 검색에 노출됩니다.")
            return redirect("console_docs")
        why = ", ".join(result.missing_fields + result.errors)
        messages.error(request, f"⛔ 적재 차단(BLOCKED): {why}")
        return redirect(back)
    finally:
        session.close()


@login_required
@require_POST
def review_cancel(request, doc_id: str):
    """검토 대기 문서 등록 취소 — 아직 색인 전이므로 문서·조각을 폐기한다."""
    session = bridge.open_session()
    try:
        svc = bridge.get_review_service(session)
        doc = svc.docs.get(doc_id)
        status = svc.docs.get_status(doc_id)
        pending = {IngestionStatus.PENDING_REVIEW.value, IngestionStatus.BLOCKED.value}
        if doc is None or status not in pending:
            return redirect("console_docs")
        if not can_review_doc(session, request.user, doc):
            messages.error(request, "이 문서를 취소할 권한이 없습니다.")
            return redirect("console_docs")
        bridge.get_document_manager(session).delete(doc_id, hard=True)
        session.commit()
        messages.info(request, "등록을 취소했습니다(문서를 폐기했습니다).")
        # 남은 검토 대기 문서가 있으면 이어서 검토
        remaining = svc.list_pending(author_id=_email_of(request.user))
        if remaining:
            return redirect(f"/console/docs/?doc={remaining[0]['doc_id']}")
        return redirect("console_docs")
    finally:
        session.close()


def _enqueue_uploads(session, files, *, uploaded_by: str,
                     folder_node_id=None) -> int:
    """올린 파일을 대기 폴더에 저장하고 처리 대기열에 넣는다. → 예약 건수.

    요청 안에서는 파싱·AI 자동 채움을 하지 않는다(문서당 수십 초라 요청이 끊긴다).
    실제 등록은 `scripts.ingest_worker` 가 처리 시간대에 맡는다.
    """
    import uuid

    from app.config import settings as app_settings
    from app.db.repositories import UploadJobRepository
    from app.ingestion.titletools import safe_filename

    batch = uuid.uuid4().hex[:12]
    queue_dir = Path(app_settings.upload_queue_dir) / batch
    queue_dir.mkdir(parents=True, exist_ok=True)
    repo = UploadJobRepository(session)

    n = 0
    for i, f in enumerate(files):
        # 파일명은 **그대로** 둔다(제목·날짜를 파일명에서 뽑으므로 접두어를 붙이면 안 된다).
        # 대신 파일마다 번호 폴더를 하나씩 줘서 같은 이름이 겹쳐도 덮어쓰지 않게 한다.
        name = Path(f.name).name                     # 경로 조작 방지(디렉터리 성분 제거)
        stem = safe_filename(Path(name).stem) or "document"
        slot = queue_dir / f"{i:04d}"
        slot.mkdir(parents=True, exist_ok=True)
        dest = slot / f"{stem}{Path(name).suffix}"
        with open(dest, "wb") as out:
            for chunk in f.chunks():
                out.write(chunk)
        repo.enqueue(path=str(dest), source_filename=f.name, uploaded_by=uploaded_by,
                     batch=batch, folder_node_id=folder_node_id)
        n += 1
    session.commit()
    return n


def _queue_status(session, uploaded_by: str, is_adm: bool) -> dict:
    """화면에 보여줄 예약 업로드 현황(관리자는 전체, 그 외는 본인 것)."""
    from datetime import datetime as _dt

    from app.config import settings as app_settings
    from app.db.repositories import UploadJobRepository
    from app.manage.schedule import describe, next_run_hint, window_from_settings

    repo = UploadJobRepository(session)
    who = None if is_adm else uploaded_by
    counts = repo.counts(who)
    start, end = window_from_settings(app_settings)
    return {
        "counts": counts,
        "pending": counts[UploadJobRepository.QUEUED] + counts[UploadJobRepository.PROCESSING],
        "failed_jobs": repo.list_jobs(uploaded_by=who,
                                      status=UploadJobRepository.FAILED, limit=20),
        "window": describe(start, end),
        "hint": next_run_hint(_dt.now(), start, end),
    }


def _visible_dup(session, drepo, visible_to, title, fmt, exclude=None):
    """제목+형식이 같은 기존 문서 — 단, **내가 볼 수 있는 것만**.

    볼 수 없는 문서를 중복으로 알려주면 '어떤 제목의 문서가 존재하는지'가 새어 나가고,
    등록까지 막히면서 그 사실이 한 번 더 확인된다. 안 보이는 문서는 없는 것으로 취급한다.
    """
    if not title or not fmt:
        return None
    dup = drepo.find_active_by_title_format(title, fmt, exclude_doc_id=exclude)
    if dup is None:
        return None
    return dup if drepo.readable_doc_ids([dup["doc_id"]], visible_to) else None


# ── 문서 관리 ────────────────────────────────────────────────────────────────
def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _dept_from_form(session, post):
    """폼의 작성부서(author_node_id=노드 id) → (node_id, 노드이름). 없으면 (None, None)."""
    from app.db.repositories import OrgRepository
    raw = post.get("author_node_id")
    if not raw:
        return None, None
    try:
        nid = int(raw)
    except (TypeError, ValueError):
        return None, None
    node = OrgRepository(session).get(nid)
    return (nid, node.name) if node is not None else (None, None)


@login_required
def docs(request):
    session = bridge.open_session()
    try:
        mgr = bridge.get_document_manager(session)
        me = _email_of(request.user)

        # 문서 등록(업로드) — 여러 파일 동시 업로드 가능. 각각 '내 검토 대기'에 올려
        # 업로더가 확인·등록한다. 첫 문서로 이동해 순차 검토를 시작한다.
        if request.method == "POST" and request.FILES.getlist("file"):
            folder_id = _int(request.POST.get("folder_node_id"), 0) or None
            # 예약 처리: 파일만 받아 대기열에 넣고 바로 응답한다(야간 워커가 등록).
            if request.POST.get("mode") == "queue":
                n = _enqueue_uploads(session, request.FILES.getlist("file"),
                                     uploaded_by=me, folder_node_id=folder_id)
                messages.success(
                    request, f"{n}건을 예약했습니다. 처리 시간대에 자동으로 등록되며, "
                    "진행 상황은 아래 '예약 업로드' 에서 확인할 수 있습니다.")
                return redirect(request.POST.get("next") or "console_docs")

            # 즉시 처리는 요청 안에서 AI가 문서를 읽는다 → 개수를 제한한다.
            # (Django 하드 한도는 예약 기준으로 커서 여기서 따로 막아야 한다)
            sync_max = getattr(django_settings, "UPLOAD_SYNC_MAX_FILES", 50)
            if len(request.FILES.getlist("file")) > sync_max:
                messages.error(
                    request, f"즉시 처리는 한 번에 {sync_max}개까지입니다. "
                    "그보다 많으면 '예약 처리'로 올려주세요(처리 시간대에 자동 등록됩니다).")
                return redirect(request.POST.get("next") or "console_docs")

            svc = bridge.get_review_service(session)
            first_id, ok_n, dups, errs = None, 0, [], []
            for f in request.FILES.getlist("file"):
                dest = Path(tempfile.gettempdir()) / f.name
                with open(dest, "wb") as out:
                    for chunk in f.chunks():
                        out.write(chunk)
                try:
                    new_id = svc.start_ingestion(str(dest), ingested_by=me,
                                                 folder_node_id=folder_id)
                    ok_n += 1
                    first_id = first_id or new_id
                except DuplicateError as e:
                    dups.append(f"{f.name}({e.reason})")
                except ReadError as e:
                    errs.append(f"{f.name}: {e}")
                except Exception:   # LLM 일시 오류 등 — 500 대신 안내 후 재시도 유도
                    errs.append(f"{f.name}: AI 처리 중 일시 오류가 발생했습니다. 잠시 후 다시 올려주세요.")
            if ok_n:
                messages.success(request, f"{ok_n}건 업로드 완료 — 내용을 확인·수정한 뒤 등록을 확정하세요.")
            if dups:
                messages.warning(request, "이미 등록된 문서: " + ", ".join(dups))
            for e in errs:
                messages.error(request, f"⚠️ {e}")
            return redirect(f"/console/docs/?doc={first_id}" if first_id else "console_docs")

        q = request.GET.get("q") or None
        status = request.GET.get("status") or None
        doc_type = request.GET.get("doc_type") or None
        page = max(1, _int(request.GET.get("page"), 1))
        offset = (page - 1) * _PAGE_SIZE
        from app.db.repositories import DocumentRepository as _DR
        sort_key = request.GET.get("sort") or "updated"
        if sort_key not in _DR.SORT_FIELDS:
            sort_key = "updated"
        sort_desc = request.GET.get("dir", "desc") != "asc"

        # 보이는 범위 = '열람 권한이 있는 문서' ∪ '부서장으로서 관리하는 문서'.
        # (예전엔 작성부서만 봐서, 상위 부서 공개 문서가 형제 파트에 안 보였다.)
        is_adm, scope = manage_scope(session, request.user)
        vis = visibility_for(session, request.user)
        can_manage = is_adm or bool(scope)

        # 폴더(조직노드) 필터 — 상위 폴더 선택 시 하위 폴더 문서까지 포함(subtree)
        from app.db.repositories import OrgRepository
        org = OrgRepository(session)
        tree = org.load_tree()
        folder_sel = _int(request.GET.get("folder"), 0) or None
        author_ids = set(tree.subtree(folder_sel)) or {-1} if folder_sel else None

        # 목록은 등록(색인) 완료 문서만. 검토 대기는 아래 '내 검토 대기'로 분리.
        total = mgr.count_documents(text=q, lifecycle_status=status, doc_type=doc_type,
                                    author_node_ids=author_ids, indexed_only=True,
                                    visible_to=vis)
        doc_list = mgr.list_documents(text=q, lifecycle_status=status, doc_type=doc_type,
                                      limit=_PAGE_SIZE, offset=offset,
                                      author_node_ids=author_ids, indexed_only=True,
                                      visible_to=vis, sort=sort_key, desc=sort_desc)
        num_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

        # 폴더 트리(각 노드의 subtree 문서 수 = 내가 볼 수 있는 것만)
        folder_tree = _org_options(org, include_folders=True)
        for n in folder_tree:
            n["doc_count"] = mgr.count_documents(
                author_node_ids=set(tree.subtree(n["id"])) or {-1},
                indexed_only=True, visible_to=vis)
            n["selected"] = (n["id"] == folder_sel)
        folder_total = mgr.count_documents(indexed_only=True, visible_to=vis)

        # 내가 올린 검토 대기 문서(등록 전 — 업로더가 확인해야 함)
        svc = bridge.get_review_service(session)
        my_pending = svc.list_pending(author_id=me)

        sel = request.GET.get("doc")
        doc = mgr.get(sel) if sel else None
        # 상세는 URL 로 직접 열 수 있으므로 여기서 반드시 열람 권한을 확인한다.
        # (검토 대기 문서는 업로더 본인이 확인해야 하므로 검토 권한도 인정)
        if doc is not None and not (can_read_doc(session, request.user, doc)
                                    or can_review_doc(session, request.user, doc)):
            messages.error(request, "이 문서를 볼 권한이 없습니다.")
            return redirect("console_docs")
        sel_status = svc.docs.get_status(sel) if sel else None
        pending_states = {IngestionStatus.PENDING_REVIEW.value, IngestionStatus.BLOCKED.value}

        # 선택 문서가 '검토 대기'이고 검토 권한이 있으면 검토 폼을 띄운다.
        review_view = None
        dup = None
        if doc is not None and sel_status in pending_states and can_review_doc(
                session, request.user, doc):
            from app.db.repositories import DocumentRepository
            from app.relations.classify import RELATION_LABELS
            from app.relations.detect import DUP_THRESHOLD
            review_view = svc.get_review(sel)
            # AI 유사 문서 탐지는 벡터 검색이라 권한을 모른다 → 여기서 걸러낸다.
            # (안 거르면 업로드만 해도 남의 부서 파일명이 후보로 노출된다)
            _seen = DocumentRepository(session).readable_doc_ids(
                [c.get("doc_id") for c in review_view.similar_candidates], vis)
            review_view.similar_candidates = [
                c for c in review_view.similar_candidates if c.get("doc_id") in _seen]
            # 개정판(교체) 후보 vs AI 추천 연관 문서로 분리
            for c in review_view.similar_candidates:
                relk = c.get("ai_relation") or "revision"
                c["ai_label"] = RELATION_LABELS.get(relk, relk)
                c["default_action"] = {"revision": "supersede", "related": "relate",
                                       "unrelated": "ignore"}.get(relk, "supersede")
            review_view.revision_candidates = [
                c for c in review_view.similar_candidates if c.get("score", 0) >= DUP_THRESHOLD]
            review_view.related_recos = [
                c for c in review_view.similar_candidates if c.get("score", 0) < DUP_THRESHOLD]
            dup = _visible_dup(session, DocumentRepository(session), vis,
                               doc.classification.title_normalized,
                               doc.identification.file_format.value, exclude=sel)
            # 제목이 파일명과 다르면 AI가 제목을 보정한 것 → 검토 폼에서 고지
            review_view.filename_stem = Path(doc.identification.source_filename).stem

        # 버전 정리: 같은 제목(날짜 접두 제외) 기준 '옛 버전 추정' 문서만 제안(없으면 검색).
        supersede_suggestions = []
        if doc is not None and review_view is None:
            from app.ingestion.titletools import strip_date_prefix
            this_base = strip_date_prefix(
                doc.classification.title_normalized or doc.identification.source_filename).lower()
            for d in mgr.list_documents(doc_type=doc.classification.doc_type.value,
                                        limit=300, indexed_only=True, visible_to=vis):
                if d["doc_id"] == sel:
                    continue
                base = strip_date_prefix(d["title"] or d["filename"]).lower()
                if base and base == this_base:
                    supersede_suggestions.append(d)

        # 권한 선택은 부서만(node_opts), 저장 위치 선택은 하위 폴더까지(folder_opts).
        node_opts = _org_options(org)
        folder_opts = folder_tree
        if doc is not None:
            sels = set(doc.governance.access_selections)
            author_node = doc.governance.author_node_id
            for n in node_opts:
                n["sel_node"] = f"node:{n['id']}" in sels
            for n in folder_opts:
                n["sel_author"] = (n["id"] == author_node)   # 지금 저장된 폴더

        # 선택한 폴더 정보(하위 폴더 만들기·기본 권한 설정용)
        sel_folder = org.get(folder_sel) if folder_sel else None
        folder_default = org.default_access_for(folder_sel) if folder_sel else []
        can_folder = (can_manage_folder(session, request.user, folder_sel)
                      if folder_sel else False)
        default_opts = _org_options(org)
        for n in default_opts:
            n["sel_default"] = f"node:{n['id']}" in set(folder_default)

        today = date.today()
        expiring_soon = mgr.repo.count_expiring_soon(
            today, today + timedelta(days=_EXPIRE_SOON_DAYS))

        # 필터 유지용 쿼리스트링(페이징 링크에 재사용)
        qs = {k: v for k, v in (("q", q), ("status", status),
                                ("doc_type", doc_type),
                                ("folder", folder_sel),
                                ("sort", sort_key if sort_key != "updated" else None),
                                ("dir", "asc" if not sort_desc else None)) if v}

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
            "supersede_suggestions": supersede_suggestions,
            "q": q or "", "status_sel": status or "", "doc_type_sel": doc_type or "",
            "sort_key": sort_key, "sort_desc": sort_desc,
            "sort_options": [("updated", "최근 변경순"), ("created", "등록일순"),
                             ("effective", "작성일순"), ("title", "제목순"),
                             ("doc_type", "문서 종류순"), ("status", "상태순")],
            "folder_tree": folder_tree, "folder_sel": folder_sel,
            "folder_total": folder_total,
            "folder_path": " / ".join(tree.name_path(folder_sel)) if folder_sel else "",
            "opts": ENUM_OPTIONS, "node_opts": node_opts,
            "folder_opts": folder_opts, "default_opts": default_opts,
            "sel_folder": sel_folder, "can_folder": can_folder,
            "statuses": USER_DOC_STATUSES, "doc_types": ENUM_OPTIONS["doc_type"],
            "total": total, "page": page, "num_pages": num_pages,
            "page_start": offset + 1 if total else 0,
            "page_end": min(offset + _PAGE_SIZE, total),
            "expiring_soon": expiring_soon, "expire_days": _EXPIRE_SOON_DAYS,
            "filter_qs": qs,
            "is_admin_user": is_adm, "can_manage": can_manage,
            "can_edit_sel": can_edit_sel, "can_delete_sel": can_delete_sel,
            "pending_requests": pending_requests,
            "admins": system_config.admin_emails(),
            "reporting_lines": system_config.reporting_lines(),
            "related": _related_docs(session, sel, vis) if doc else [],
            "queue": _queue_status(session, me, is_adm),
        })
    finally:
        session.close()


@login_required
@require_POST
def queue_action(request):
    """예약 업로드 대기열 조작 — 실패분 재시도 / 완료 기록 지우기.

    관리자는 전체, 그 외에는 **본인이 올린 것만** 대상으로 한다.
    """
    from app.db.repositories import UploadJobRepository

    session = bridge.open_session()
    try:
        me = _email_of(request.user)
        is_adm, _ = manage_scope(session, request.user)
        who = None if is_adm else me
        repo = UploadJobRepository(session)
        action = request.POST.get("action")
        if action == "retry":
            n = repo.retry_failed(who)
            messages.success(request, f"실패한 {n}건을 다시 대기열에 넣었습니다."
                             if n else "다시 시도할 실패 건이 없습니다.")
        elif action == "clear":
            n = repo.clear_done(who)
            messages.info(request, f"완료 기록 {n}건을 지웠습니다.")
        else:
            messages.error(request, "알 수 없는 요청입니다.")
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
def docs_search(request):
    """문서명(제목·파일명) 검색 → JSON {id,label,sub}. 연관/버전 지정 자동완성용.

    **열람 권한이 있는 문서만** 돌려준다. 이 자동완성이 권한을 안 걸면 제목·파일명이
    전 직원에게 노출되어 접근 통제가 사실상 무력해진다.
    """
    from django.http import JsonResponse
    session = bridge.open_session()
    try:
        q = (request.GET.get("q") or "").strip()
        exclude = request.GET.get("exclude")
        if not q:
            return JsonResponse({"results": []})
        mgr = bridge.get_document_manager(session)
        rows = mgr.list_documents(text=q, indexed_only=True, limit=20,
                                  visible_to=visibility_for(session, request.user))
        out = [{"id": r["doc_id"], "label": r["title"] or r["filename"],
                "sub": r["filename"]}
               for r in rows if r["doc_id"] != exclude]
        return JsonResponse({"results": out})
    finally:
        session.close()


@login_required
def users_search(request):
    """사람 검색 → JSON {id,label,sub}. 동명이인 구분 위해 이름+아이디를 함께 준다."""
    from django.http import JsonResponse
    from app.db.repositories import UserRepository
    session = bridge.open_session()
    try:
        q = (request.GET.get("q") or "").strip().lower()
        out = []
        for u in UserRepository(session).list_users():
            name = u.get("display_name") or ""
            uid = u["user_id"]
            if not q or q in uid.lower() or q in name.lower():
                out.append({"id": uid, "label": name or uid, "sub": uid})
            if len(out) >= 20:
                break
        return JsonResponse({"results": out})
    finally:
        session.close()


@login_required
@require_POST
def docs_bulk_delete(request):
    """선택한 여러 문서를 한 번에 삭제(각 문서마다 삭제 권한 확인)."""
    session = bridge.open_session()
    try:
        mgr = bridge.get_document_manager(session)
        ids = request.POST.getlist("doc_ids")
        deleted, skipped = 0, 0
        for doc_id in ids:
            doc = mgr.get(doc_id)
            if doc is not None and can_delete_doc(session, request.user, doc):
                mgr.delete(doc_id, hard=True)
                deleted += 1
            else:
                skipped += 1
        if deleted:
            messages.warning(request, f"{deleted}건을 영구 삭제했습니다." +
                             (f" ({skipped}건은 권한 없음으로 제외)" if skipped else ""))
        elif skipped:
            messages.error(request, "삭제 권한이 있는 문서가 없습니다. '삭제 요청'을 이용하세요.")
        else:
            messages.warning(request, "선택된 문서가 없습니다.")
        return redirect(request.POST.get("next") or "console_docs")
    finally:
        session.close()


@login_required
@require_POST
def folders(request):
    """하위 폴더 만들기·이름변경·삭제·기본권한 설정 + (관리자) 디스크 동기화.

    폴더는 저장·분류용이라 부서 구성원도 만들 수 있다. **권한 주체는 아니다** —
    폴더에 설정하는 건 '이 폴더에 올릴 때 채워질 열람 권한 기본값'일 뿐이다.
    """
    session = bridge.open_session()
    try:
        from app.db.repositories import OrgRepository
        from app.manage.storage import sync_with_disk
        from app.org.tree import FOLDER
        from web.authz import can_manage_folder
        org = OrgRepository(session)
        p = request.POST
        action = p.get("action")
        back = p.get("next") or "console_docs"

        if action == "sync":                      # 디스크 ↔ 화면 동기화(관리자 전용)
            if not is_admin(request.user):
                messages.error(request, "폴더 동기화는 관리자만 할 수 있습니다.")
                return redirect(back)
            registered, created = sync_with_disk(session)
            session.commit()
            if registered:
                messages.success(request, f"서버에 있던 폴더 {len(registered)}개를 등록했습니다: "
                                 + ", ".join(registered[:5])
                                 + (" 외" if len(registered) > 5 else ""))
            if created:
                messages.info(request, f"화면에만 있던 폴더 {len(created)}개를 서버에 만들었습니다.")
            if not registered and not created:
                messages.info(request, "이미 서버와 화면이 같습니다.")
            return redirect(back)

        node_id = _int(p.get("node_id"), 0) or None
        if action == "create":
            name = (p.get("name") or "").strip()
            if not name:
                messages.error(request, "폴더 이름을 입력하세요.")
                return redirect(back)
            if not can_manage_folder(session, request.user, node_id):
                messages.error(request, "이 부서에 폴더를 만들 권한이 없습니다.")
                return redirect(back)
            new = org.create_node(name, FOLDER, parent_id=node_id)
            session.flush()
            sync_with_disk(session, new.id)       # 디스크에도 바로 만든다
            session.commit()
            messages.success(request, f"'{name}' 폴더를 만들었습니다.")
            return redirect(f"/console/docs/?folder={new.id}")

        node = org.get(node_id) if node_id else None
        if node is None:
            return redirect(back)
        if not can_manage_folder(session, request.user, node_id):
            messages.error(request, "이 폴더를 관리할 권한이 없습니다.")
            return redirect(back)

        if action == "rename":
            if node.node_type != FOLDER:
                messages.error(request, "부서 이름은 조직도에서 바꿔주세요.")
                return redirect(back)
            name = (p.get("name") or "").strip()
            if name:
                from app.manage.storage import relocate_subtree
                org.rename_node(node_id, name)
                session.commit()
                moved = relocate_subtree(session, node_id)
                sync_with_disk(session, node_id)
                session.commit()
                messages.success(request, f"폴더 이름을 바꿨습니다." +
                                 (f" (파일 {moved}건 이동)" if moved else ""))
        elif action == "delete":
            if node.node_type != FOLDER:
                messages.error(request, "부서는 조직도에서 삭제해주세요.")
                return redirect(back)
            ids = set(org.load_tree().subtree(node_id))
            n_docs = bridge.get_document_manager(session).count_documents(
                author_node_ids=ids) if ids else 0
            if n_docs:
                messages.error(request, f"이 폴더(하위 포함)에 문서 {n_docs}건이 있어 삭제할 수 "
                               "없습니다. 문서를 다른 폴더로 옮긴 뒤 다시 시도하세요.")
                return redirect(back)
            org.delete_node(node_id)
            session.commit()
            messages.warning(request, "폴더를 삭제했습니다(서버 디렉터리는 그대로 두었습니다).")
            return redirect("console_docs")
        elif action == "default_access":
            org.set_default_access(node_id, p.getlist("access"))
            session.commit()
            messages.success(request, "이 폴더에 올릴 때의 기본 열람 권한을 저장했습니다.")
        return redirect(back)
    finally:
        session.close()


@login_required
@require_POST
def docs_bulk_update(request):
    """선택한 여러 문서의 열람 권한·작성부서·상태·종류를 한 번에 바꾼다.

    같은 부서에서 올린 문서라도 파일마다 권한이 달라야 하는 경우가 많아서, 목록에서
    골라 한 번에 적용할 수 있어야 한다. 문서마다 **수정 권한을 개별 확인**한다.
    """
    session = bridge.open_session()
    try:
        mgr = bridge.get_document_manager(session)
        p = request.POST
        ids = p.getlist("doc_ids")
        field = p.get("field")            # access | folder | status | doc_type
        if not ids:
            messages.warning(request, "선택된 문서가 없습니다.")
            return redirect(p.get("next") or "console_docs")

        node_id, dept_name = _dept_from_form(session, p)
        access = p.getlist("access")
        status = p.get("lifecycle_status")
        doc_type = p.get("doc_type")
        if field == "folder" and node_id is None:
            messages.error(request, "옮길 폴더를 고르세요.")
            return redirect(p.get("next") or "console_docs")
        if field == "doc_type" and doc_type not in ENUM_OPTIONS["doc_type"]:
            messages.error(request, "문서 종류를 고르세요.")
            return redirect(p.get("next") or "console_docs")
        if field == "status" and status not in USER_DOC_STATUSES:
            messages.error(request, "상태를 고르세요.")
            return redirect(p.get("next") or "console_docs")

        done, skipped = 0, 0
        for doc_id in ids:
            doc = mgr.get(doc_id)
            if doc is None or not can_edit_doc(session, request.user, doc):
                skipped += 1
                continue
            gov, cls_over, life_over = doc.governance, None, None
            if field == "access":
                # 빈 선택 = 팀 전체 공개. 의도적으로 지울 수 있어야 하므로 그대로 반영.
                gov = gov.model_copy(update={"access_selections": access})
            elif field == "folder":
                gov = gov.model_copy(update={"author_node_id": node_id})
                cls_over = {"department": dept_name}
            elif field == "status":
                life_over = {"status": status}
            elif field == "doc_type":
                cls_over = {"doc_type": doc_type}
            else:
                messages.error(request, "무엇을 바꿀지 고르세요.")
                return redirect(p.get("next") or "console_docs")

            mgr.update_metadata(doc_id, governance=gov,
                                classification_overrides=cls_over,
                                lifecycle_overrides=life_over)
            if field == "folder":        # 폴더가 바뀌면 원본 파일도 옮긴다
                try:
                    from pathlib import Path as _P
                    from app.manage.storage import place
                    cur = mgr.repo.get_original_path(doc_id)
                    if cur:
                        place(session, doc_id, node_id,
                              doc.classification.title_normalized or _P(cur).stem,
                              _P(cur).suffix)
                except Exception:
                    pass
            done += 1
        session.commit()

        label = {"access": "열람 권한", "folder": "폴더(작성부서)",
                 "status": "상태", "doc_type": "문서 종류"}.get(field, "항목")
        if done:
            messages.success(request, f"{done}건의 {label}을(를) 변경했습니다." +
                             (f" ({skipped}건은 수정 권한 없음으로 제외)" if skipped else ""))
        else:
            messages.error(request, "수정 권한이 있는 문서가 없습니다.")
        return redirect(p.get("next") or "console_docs")
    finally:
        session.close()


@login_required
@require_POST
def docs_action(request, doc_id: str):
    session = bridge.open_session()
    try:
        mgr = bridge.get_document_manager(session)
        doc = mgr.get(doc_id)
        if doc is None or not can_read_doc(session, request.user, doc):
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
            if p.get("doc_type") and p["doc_type"] not in ENUM_OPTIONS["doc_type"]:
                messages.error(request, "문서 종류를 골라주세요.")
                return redirect(f"/console/docs/?doc={doc_id}")
            gov = doc.governance
            # 작성부서(조직 노드) 변경 반영. 미선택이면 기존 값 유지.
            node_id, dept_name = _dept_from_form(session, p)
            if node_id is None:
                node_id, dept_name = gov.author_node_id, doc.classification.department
            new_gov = GovernanceBlock(
                access_selections=p.getlist("access"),
                author_id=gov.author_id, author_name=gov.author_name,
                author_node_id=node_id,
                reporting_line=[x for x in p.getlist("reporting_line") if x])
            cls = {"doc_type": p.get("doc_type") or doc.classification.doc_type.value,
                   "title_normalized": p.get("title") or None,
                   "summary": p.get("summary") or None,
                   "department": dept_name,
                   "keywords": [k.strip() for k in (p.get("keywords") or "").split(",") if k.strip()],
                   "related_parties": [x.strip() for x in (p.get("related_parties") or "").split(",") if x.strip()]}
            life = {"status": p.get("lifecycle_status"),
                    "effective_date": _to_iso(p.get("effective_date")),
                    "expiry_date": _to_iso(p.get("expiry_date"))}
            mgr.update_metadata(doc_id, governance=new_gov,
                                classification_overrides=cls, lifecycle_overrides=life)
            # 폴더(작성부서)나 제목이 바뀌었으면 원본 파일도 해당 폴더 경로로 이동
            try:
                from pathlib import Path as _P
                from app.manage.storage import place
                cur = mgr.repo.get_original_path(doc_id)
                if cur:
                    place(session, doc_id, node_id,
                          cls["title_normalized"] or _P(cur).stem, _P(cur).suffix)
                    session.commit()
            except Exception:
                pass
            messages.success(request, "저장 완료 — 검색에 즉시 반영되었습니다.")
        elif action == "supersede":
            old = request.POST.get("old_id")
            if old:
                mgr.supersede(old, doc_id)
                messages.success(request, "옛 버전을 검색에서 제외했습니다.")
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
        elif request.POST.get("action") == "add" and other:
            # 볼 수 없는 문서는 연결도 불가 — 연결하면 상세 화면에 제목이 드러난다.
            target = mgr.get(other)
            if target is None or not can_read_doc(session, request.user, target):
                messages.error(request, "연결할 수 없는 문서입니다(권한 없음).")
            else:
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
        if doc is None or not can_read_doc(session, request.user, doc):
            messages.error(request, "이 문서에 대한 권한이 없습니다.")
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
def _org_options(org, include_folders: bool = False) -> list[dict]:
    """조직도를 트리 순서(깊이 포함)로 평탄화 — 드롭다운·표 들여쓰기용.

    include_folders=False 가 기본이다. **열람 권한은 부서 단위로만** 고르게 해야 하므로
    권한 드롭다운에 하위 폴더가 나오면 안 된다. 폴더 트리·저장 위치 선택처럼 폴더가
    필요한 곳에서만 True 로 부른다.
    """
    from app.org.tree import is_folder
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
        if is_folder(node.node_type) and not include_folders:
            return
        out.append({"id": node.id, "name": node.name, "node_type": node.node_type,
                    "is_folder": is_folder(node.node_type),
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
                    node_id = int(p["node_id"])
                    repo.rename_node(node_id, name)
                    session.commit()
                    # 폴더 이름이 바뀌었으니 디스크의 저장 경로도 재정렬
                    from app.manage.storage import relocate_subtree
                    moved = relocate_subtree(session, node_id)
                    session.commit()
                    extra = f" (파일 {moved}건 이동)" if moved else ""
                    messages.success(request, f"이름을 변경했습니다.{extra}")
            elif action == "delete":
                node_id = int(p["node_id"])
                # 폴더에 문서가 있으면 삭제 차단(먼저 비우도록 안내)
                ids = set(repo.load_tree().subtree(node_id))
                n_docs = bridge.get_document_manager(session).count_documents(
                    author_node_ids=ids) if ids else 0
                if n_docs:
                    messages.error(
                        request, f"이 조직(하위 포함)에 문서 {n_docs}건이 있어 삭제할 수 없습니다. "
                        "문서를 다른 폴더로 옮기거나 삭제한 뒤 다시 시도하세요.")
                    return redirect("console_org")
                repo.delete_node(node_id)
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
