"""골드셋 평가 실행 (검색 지표 + 접근통제 회귀 + LLM-as-judge).

전제: smoke 로 샘플이 이미 색인돼 있어야 하고, 실제 서비스(vLLM/TEI/Qdrant/Postgres)가 떠 있어야 한다.
    python -m scripts.smoke --samples-dir samples     # 먼저 색인
    python -m scripts.eval --goldset samples/goldset.json

접근통제 위반이 하나라도 있으면 종료 코드 1(회귀 실패).
"""

from __future__ import annotations

import argparse
import sys

from app.clients.embedding import TEIEmbedder
from app.clients.llm import VLLMClient
from app.clients.qdrant_search import QdrantDenseSearch
from app.clients.reranker import TEIReranker
from app.db.repositories import AuditRepository, UserRepository
from app.db.session import create_all, make_engine, make_session_factory
from app.eval.goldset import load_goldset
from app.eval.runner import run_eval
from app.search.pipeline import SearchPipeline
from app.search.retriever import HybridRetriever


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--goldset", default="samples/goldset.json")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--judge", action="store_true", help="LLM-as-judge 채점 포함")
    args = ap.parse_args()

    engine = make_engine()
    create_all(engine)
    session = make_session_factory(engine)()
    users = UserRepository(session)

    pipeline = SearchPipeline(
        retriever=HybridRetriever(dense=QdrantDenseSearch(embedder=TEIEmbedder())),
        reranker=TEIReranker(),
        llm=VLLMClient(),
        audit=AuditRepository(session),
        top_k=args.k,
    )

    goldset = load_goldset(args.goldset)
    report = run_eval(
        pipeline, goldset,
        user_resolver=users.get_user_context,
        k=args.k,
        judge=VLLMClient() if args.judge else None,
    )
    session.commit()

    print(f"\n=== 평가 결과 (k={report.k}, n={len(report.results)}) ===")
    print(f"Recall@{report.k}: {report.mean_recall:.3f}   "
          f"MRR: {report.mean_mrr:.3f}   nDCG@{report.k}: {report.mean_ndcg:.3f}")
    print(f"답변 정확도(기대문자열): {report.answer_accuracy:.3f}")
    if args.judge:
        print(f"groundedness: {report.mean_groundedness:.3f}   "
              f"relevance: {report.mean_relevance:.3f}")
    print(f"접근통제 위반: {report.total_access_violations}건")

    print("\n항목별:")
    for r in report.results:
        flag = "  ⛔ 위반" if r.access_violations else ""
        print(f"  [{r.id}] R@k={r.recall_at_k:.2f} MRR={r.mrr:.2f} "
              f"ans_ok={r.answer_ok}{flag}")
        if r.access_violations:
            print(f"      노출된 금지문서: {r.access_violations}")

    if not report.passed:
        print("\n❌ 접근통제 회귀 실패 — 금지 문서가 노출/인용됨")
        return 1
    print("\n✅ 접근통제 회귀 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
