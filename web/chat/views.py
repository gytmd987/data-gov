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


def _folder_scope(session, folder_id):
    """폴더 스코프: 상위 폴더를 고르면 하위 폴더 문서까지 포함(subtree)."""
    if not folder_id:
        return None
    from app.db.repositories import OrgRepository
    try:
        return set(OrgRepository(session).load_tree().subtree(int(folder_id)))
    except (TypeError, ValueError):
        return None


def run_turn(request, body):
    """한 번의 질문 처리 — 이벤트 생성기.

    스트리밍(`/chat/stream`)과 한 번에 받기(`/chat/send`)가 **같은 이 함수**를 쓴다.
    경로를 둘로 나눠 두면 한쪽만 고쳐져 조용히 어긋난다.

    이벤트: `step`(진행 상황) · `delta`(답변 조각) · `done`(최종) · `error`
    """
    text = (body.get("text") or "").strip()
    use_rag = bool(body.get("use_rag"))
    include_past = bool(body.get("include_past"))
    plan = body.get("plan", True)            # 판단 루프(자세히 찾기)
    conv_id = body.get("conversation_id")
    if not text:
        yield {"type": "error", "error": "질문이 비어 있습니다.", "status": 400}
        return

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
                yield {"type": "error", "status": 403,
                       "error": f"'{email}' 사용자의 권한 정보가 없습니다. "
                                "관리자에게 사용자 등록을 요청하세요."}
                return
            from app.search.present import group_sources, renumber_citations
            pipe = bridge.get_search_pipeline(session)
            today = date.today()
            folder_ids = _folder_scope(session, body.get("folder_node_id"))

            def _dataset_route(doc_ids):
                """표 데이터(엑셀)가 잡혔으면 SQL 로 정확히 답한다 — 문장 생성 대신.

                문장을 다 만든 뒤에 갈아 끼우면 그 생성 시간이 통째로 낭비고, 화면에서도
                답이 흘러나오다가 전혀 다른 답으로 바뀌어 보인다.
                """
                from app.datasets.query import maybe_answer_structured
                return maybe_answer_structured(session, bridge.get_chat_llm(),
                                               user_ctx, text, doc_ids)

            ans = None
            for event in pipe.answer_events(
                    text, user_ctx, session=session, today=today,
                    include_past=include_past, folder_node_ids=folder_ids,
                    plan=bool(plan), preempt=_dataset_route):
                if event["type"] == "answer":
                    ans = event["answer"]
                elif event["type"] == "preempted":
                    dataset_answer = event["result"]
                else:
                    yield event                      # step / delta 는 그대로 흘린다

            if dataset_answer is not None:
                answer_text = dataset_answer["text"]
                yield {"type": "delta", "text": answer_text}
            else:
                sources = group_sources(ans, today=today)
                # 본문의 인용 번호를 화면의 출처 번호와 일치시킨다(어긋나면 헷갈린다)
                answer_text = renumber_citations(ans.text, sources)
                _attach_related(session, sources, user_ctx, today)
        else:
            hist = chat.get_messages(conv_id, user_id=email, limit=_HISTORY_TURNS)
            llm = bridge.get_chat_llm()
            prompt = _plain_prompt(hist, text)
            streamer = getattr(llm, "stream_text", None)
            if streamer is None:
                answer_text = llm.complete_text(prompt)
            else:
                answer_text = ""
                for piece in streamer(prompt):
                    answer_text += piece
                    yield {"type": "delta", "text": piece}

        chat.add_message(conv_id, "user", text, use_rag=use_rag)
        msg_id = chat.add_message(conv_id, "assistant", answer_text,
                                  use_rag=use_rag, sources=sources)
        session.commit()
        yield {"type": "done", "conversation_id": conv_id, "message_id": msg_id,
               "text": answer_text, "sources": sources,
               "dataset_answer": dataset_answer}
    finally:
        session.close()


@login_required
@require_POST
def send(request):
    """한 번에 받기 — 이벤트를 다 흘려보내고 마지막 결과만 JSON 으로 돌려준다.

    스트리밍을 못 쓰는 호출자(스모크·스크립트)를 위해 남겨 둔다.
    """
    body = json.loads(request.body or "{}")
    for event in run_turn(request, body):
        if event["type"] == "error":
            return JsonResponse({"error": event["error"]},
                                status=event.get("status", 400))
        if event["type"] == "done":
            return JsonResponse({k: v for k, v in event.items() if k != "type"})
    return JsonResponse({"error": "답변을 만들지 못했습니다."}, status=500)


@login_required
@require_POST
def stream(request):
    """스트리밍 — 진행 상황과 답변 조각을 도착하는 대로 내보낸다(SSE).

    답변 생성에 4~9초가 걸리는데 다 만든 뒤 한 번에 주면 그동안 화면이 멈춰 보인다.
    """
    from django.http import StreamingHttpResponse

    body = json.loads(request.body or "{}")

    def events():
        try:
            for event in run_turn(request, body):
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        except Exception as e:      # 생성 도중 죽어도 화면은 이유를 받아야 한다
            yield "data: " + json.dumps(
                {"type": "error", "error": f"처리 중 오류: {type(e).__name__}"},
                ensure_ascii=False) + "\n\n"

    resp = StreamingHttpResponse(events(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"      # nginx 가 버퍼링하면 스트리밍이 무의미해진다
    return resp


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
