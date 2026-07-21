"""대화형 질의 CLI (실서비스).

색인된 문서에 대해 특정 사용자 권한으로 질문하고 답변+출처를 확인한다.
접근통제가 적용되므로, 사용자의 그룹/clearance에 따라 볼 수 있는 문서가 달라진다.

    # 한 번 질문
    python -m scripts.ask --user hr_lead "부장 직급의 연봉 밴드는?"

    # 대화형(질문 없이 실행 → 반복 입력, 빈 줄/exit 로 종료)
    python -m scripts.ask --user hr_analyst

사용자는 미리 존재해야 한다(예: scripts.smoke 가 hr_analyst/hr_lead 시드).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from app.clients.embedding import TEIEmbedder
from app.clients.llm import VLLMClient
from app.clients.qdrant_search import QdrantDenseSearch
from app.clients.reranker import TEIReranker
from app.db.repositories import AuditRepository, FeedbackRepository, UserRepository
from app.db.session import create_all, make_engine, make_session_factory
from app.search.pipeline import SearchPipeline
from app.search.retriever import HybridRetriever


def build_pipeline(session) -> SearchPipeline:
    return SearchPipeline(
        retriever=HybridRetriever(dense=QdrantDenseSearch(embedder=TEIEmbedder())),
        reranker=TEIReranker(),
        llm=VLLMClient(),
        audit=AuditRepository(session),
    )


def ask_once(pipe: SearchPipeline, session, user, query: str):
    ans = pipe.answer(query, user, today=date.today())
    session.commit()
    files = sorted({c.source_filename for c in ans.used_chunks if c.source_filename})
    print(f"\nA: {ans.text}")
    print(f"검색된 문서: {files or '(권한 내 근거 없음)'}")
    if ans.citations:
        cites = "; ".join(f"[{c.marker}] {c.title or c.doc_id} p.{c.page_no}"
                          for c in ans.citations)
        print(f"출처: {cites}")
    return ans


def _record_feedback(session, user, query, ans, rating, note=None):
    FeedbackRepository(session).record(
        query_text=query, rating=rating, user_id=user.user_id,
        answer_text=ans.text if ans else None, note=note,
        cited_doc_ids=[c.doc_id for c in ans.citations] if ans else [])
    session.commit()
    print("  피드백 기록됨. 감사합니다." if rating == "up"
          else "  오답 피드백 기록됨(관리자 검토 대상).")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="hr_lead", help="질의 주체 user_id (DB에 존재해야 함)")
    ap.add_argument("query", nargs="*", help="질문(생략 시 대화형 모드)")
    args = ap.parse_args()

    engine = make_engine()
    create_all(engine)
    session = make_session_factory(engine)()

    user = UserRepository(session).get_user_context(args.user)
    if user is None:
        print(f"사용자 '{args.user}' 없음. 먼저 `python -m scripts.smoke --samples-dir samples`로 "
              f"시드하거나 사용자를 생성하세요.", file=sys.stderr)
        return 1

    groups = ", ".join(sorted(user.groups))
    print(f"사용자: {user.user_id}  (그룹: {groups}, clearance: {user.clearance.value})")
    pipe = build_pipeline(session)

    if args.query:
        ask_once(pipe, session, user, " ".join(args.query))
        return 0

    print("대화형 모드 — 질문 입력(빈 줄/exit 로 종료).")
    print("답변 후 피드백:  !good  또는  !bad <무엇이 틀렸는지/정답>")
    last_q, last_ans = None, None
    while True:
        try:
            line = input("\nQ> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line or line.lower() in {"exit", "quit"}:
            break
        if line.startswith("!good"):
            _record_feedback(session, user, last_q, last_ans, "up")
            continue
        if line.startswith("!bad"):
            _record_feedback(session, user, last_q, last_ans, "down",
                             note=line[4:].strip() or None)
            continue
        last_q = line
        last_ans = ask_once(pipe, session, user, line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
