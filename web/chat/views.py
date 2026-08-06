"""채팅 뷰 — ChatGPT 스타일: 대화 목록/멀티턴/RAG 토글/피드백/원본 다운로드.

문서 등록(submit)은 일반 사용자도 가능하다(업로드 → 관리자 검토 → 색인).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date
from pathlib import Path

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from web import bridge
from web.authz import _email_of, domain_user_context

from app.ingestion.enrichment import ReadError
from app.ingestion.intake import DuplicateError

_HISTORY_TURNS = 10   # 일반 채팅 멀티턴 문맥으로 넣을 최근 메시지 수


@login_required
def chat_page(request):
    session = bridge.open_session()
    try:
        from app.db.repositories import ChatRepository, OrgRepository
        convs = ChatRepository(session).list_conversations(_email_of(request.user))
        # 검색 범위(폴더) 선택용 조직도 목록
        from web.console.views import _org_options
        folders = _org_options(OrgRepository(session))
    finally:
        session.close()
    return render(request, "chat.html", {"conversations": convs,
                                         "folders": folders,
                                         "offline": bridge.is_offline()})


@login_required
def history(request, conversation_id: int):
    session = bridge.open_session()
    try:
        from app.db.repositories import ChatRepository
        msgs = ChatRepository(session).get_messages(
            conversation_id, user_id=_email_of(request.user))
    finally:
        session.close()
    return JsonResponse({"messages": msgs})


def _attach_related(session, sources: list, user_ctx, today) -> None:
    """각 출처 문서의 연관 문서를 붙인다(사용자가 열람 가능한 것만 — 파일명 유출 방지)."""
    from app.db.repositories import DocumentRepository, RelationRepository
    from app.schemas.metadata import doc_level_payload
    from app.search.access import AccessPolicy
    rel = RelationRepository(session)
    drepo = DocumentRepository(session)
    policy = AccessPolicy.for_user(user_ctx, today=today)
    for s in sources:
        doc_id = s.get("doc_id")
        if not doc_id:
            continue
        items = []
        for r in rel.related_ids(doc_id):
            d = drepo.get(r["doc_id"])
            if d is not None and policy.allows(doc_level_payload(d)):
                items.append({"doc_id": r["doc_id"],
                              "filename": d.identification.source_filename})
        if items:
            s["related"] = items


def _plain_prompt(history_msgs, text: str) -> str:
    lines = ["당신은 사내 어시스턴트입니다. 한국어로 간결하고 정확하게 답하세요.\n"]
    for m in history_msgs:
        who = "사용자" if m["role"] == "user" else "어시스턴트"
        lines.append(f"{who}: {m['text']}")
    lines.append(f"사용자: {text}")
    lines.append("어시스턴트:")
    return "\n".join(lines)


@login_required
@require_POST
def send(request):
    body = json.loads(request.body or "{}")
    text = (body.get("text") or "").strip()
    use_rag = bool(body.get("use_rag"))
    include_past = bool(body.get("include_past"))
    conv_id = body.get("conversation_id")
    if not text:
        return JsonResponse({"error": "질문이 비어 있습니다."}, status=400)

    email = _email_of(request.user)
    session = bridge.open_session()
    try:
        from app.db.repositories import ChatRepository
        chat = ChatRepository(session)
        owned = {c["id"] for c in chat.list_conversations(email)}
        if not conv_id or conv_id not in owned:   # 소유자 검증(남의 대화 차단)
            conv_id = chat.create_conversation(email)

        sources: list = []
        dataset_answer = None
        if use_rag:
            user_ctx = domain_user_context(session, request.user)
            if user_ctx is None:
                return JsonResponse({"error": f"'{email}' 사용자의 권한 정보가 없습니다. "
                                     "관리자에게 사용자 등록을 요청하세요."}, status=403)
            from app.search.present import group_sources, renumber_citations
            pipe = bridge.get_search_pipeline(session)
            today = date.today()
            # 폴더 스코프: 상위 폴더를 고르면 하위 폴더 문서까지 포함(subtree)
            folder_ids = None
            folder_id = body.get("folder_node_id")
            if folder_id:
                from app.db.repositories import OrgRepository
                try:
                    folder_ids = set(OrgRepository(session).load_tree().subtree(int(folder_id)))
                except (TypeError, ValueError):
                    folder_ids = None
            ans = pipe.answer(text, user_ctx, today=today, include_past=include_past,
                              folder_node_ids=folder_ids)
            sources = group_sources(ans, today=today)
            # 본문의 인용 번호를 화면의 출처 번호와 일치시킨다(어긋나면 헷갈린다)
            answer_text = renumber_citations(ans.text, sources)
            _attach_related(session, sources, user_ctx, today)
            # 표 데이터가 검색에 잡히면 구조화(SQL) 답변 시도 → 정확 조회·집계
            try:
                from app.datasets.query import maybe_answer_structured
                cited = [s.get("doc_id") for s in sources if s.get("doc_id")]
                da = maybe_answer_structured(session, bridge.get_chat_llm(),
                                             user_ctx, text, cited)
                if da is not None:
                    dataset_answer = da
                    answer_text = da["text"]
            except Exception:
                pass
        else:
            hist = chat.get_messages(conv_id, user_id=email, limit=_HISTORY_TURNS)
            llm = bridge.get_chat_llm()
            answer_text = llm.complete_text(_plain_prompt(hist, text))

        chat.add_message(conv_id, "user", text, use_rag=use_rag)
        msg_id = chat.add_message(conv_id, "assistant", answer_text,
                                  use_rag=use_rag, sources=sources)
        session.commit()
        return JsonResponse({"conversation_id": conv_id, "message_id": msg_id,
                             "text": answer_text, "sources": sources,
                             "dataset_answer": dataset_answer})
    finally:
        session.close()


@login_required
@require_POST
def feedback(request):
    body = json.loads(request.body or "{}")
    session = bridge.open_session()
    try:
        from app.db.repositories import FeedbackRepository
        FeedbackRepository(session).record(
            query_text=body.get("query", ""), rating=body.get("rating", "down"),
            user_id=_email_of(request.user), answer_text=body.get("answer"),
            note=body.get("note") or None,
            cited_doc_ids=body.get("cited_doc_ids") or [])
        session.commit()
    finally:
        session.close()
    return JsonResponse({"ok": True})


@login_required
def submit_document(request):
    """문서 등록 — 업로드하면 즉시 색인되어 검색에 바로 반영된다(검토 대기 없음).

    등록·관리·수정은 '문서' 탭(/console/docs/)으로 일원화됐다. 이 경로는 하위호환.
    """
    if request.method == "POST" and request.FILES.get("file"):
        f = request.FILES["file"]
        session = bridge.open_session()
        try:
            svc = bridge.get_review_service(session)
            dest = Path(tempfile.gettempdir()) / f.name
            with open(dest, "wb") as out:
                for chunk in f.chunks():
                    out.write(chunk)
            try:
                new_id = svc.start_ingestion(str(dest), ingested_by=_email_of(request.user))
                messages.success(request, f"'{f.name}' 업로드 완료 — AI가 채운 내용을 확인·수정한 뒤 등록을 확정하세요.")
                return redirect(f"/console/docs/?doc={new_id}")
            except DuplicateError as e:
                messages.warning(request, f"'{f.name}' 은 이미 등록된 문서입니다({e.reason}).")
            except ReadError as e:
                messages.error(request, f"⚠️ '{f.name}' {e}")
        finally:
            session.close()
        return redirect("console_docs")
    return redirect("console_docs")


@login_required
def original_file(request, doc_id: str):
    """원본 파일 다운로드 — 열람 권한 재검증 후 제공."""
    session = bridge.open_session()
    try:
        from web.authz import can_read_doc
        from app.db.repositories import DocumentRepository
        repo = DocumentRepository(session)
        doc = repo.get(doc_id)
        if doc is None:
            raise Http404
        if not can_read_doc(session, request.user, doc):
            raise Http404   # 권한 없음도 404로(존재 노출 방지)
        path = repo.get_original_path(doc_id)
        if not path or not os.path.exists(path):
            raise Http404
        # 다운로드 파일명 = 제목(+원본 확장자). 제목이 없으면 원본 파일명.
        from pathlib import Path as _P
        from app.ingestion.titletools import safe_filename
        title = (doc.classification.title_normalized or "").strip()
        ext = _P(path).suffix or _P(doc.identification.source_filename).suffix
        download_name = f"{safe_filename(title)}{ext}" if title else doc.identification.source_filename
        return FileResponse(open(path, "rb"), as_attachment=True, filename=download_name)
    finally:
        session.close()
