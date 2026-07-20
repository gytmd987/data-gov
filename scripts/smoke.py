"""엔드투엔드 스모크 테스트.

실제 서비스(vLLM / TEI 임베딩·리랭커 / Qdrant / Postgres)가 떠 있어야 한다.
샘플 문서를 적재→검토(자동 승인)→색인하고, 서로 다른 권한의 두 사용자로 질의해
접근통제·인용이 실제로 동작하는지 확인한다.

실행:
    docker compose up -d          # Qdrant/Postgres/MinIO/TEI
    # vLLM은 별도로 이미 기동되어 있어야 함 (.env의 VLLM_BASE_URL)
    python -m scripts.smoke --samples-dir samples

각 단계 결과와 두 사용자의 답변을 출력한다. 종료 코드 0 = 성공.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.clients.embedding import TEIEmbedder
from app.clients.llm import VLLMClient
from app.clients.qdrant_indexer import QdrantIndexer
from app.clients.qdrant_search import QdrantDenseSearch
from app.clients.reranker import TEIReranker
from app.config import settings
from app.db.repositories import AuditRepository, UserRepository
from app.db.session import create_all, make_engine, make_session_factory
from app.ingestion.intake import DuplicateError
from app.review.service import ReviewService
from app.schemas.enums import PiiType, SensitivityLevel
from app.schemas.metadata import GovernanceBlock
from app.search.access import UserContext
from app.search.pipeline import SearchPipeline
from app.search.retriever import HybridRetriever

# 파일명 → 검토자가 확정할 거버넌스(데모용 자동 승인 매핑)
GOVERNANCE = {
    "salary": GovernanceBlock(
        sensitivity_level=SensitivityLevel.RESTRICTED, contains_pii=True,
        pii_types=[PiiType.SALARY], access_groups=["payroll"], owner="hr.lead"),
    "_default": GovernanceBlock(
        sensitivity_level=SensitivityLevel.INTERNAL, contains_pii=False,
        access_groups=["hr_core"], owner="hr.manager"),
}


def _governance_for(filename: str) -> GovernanceBlock:
    for key, gov in GOVERNANCE.items():
        if key != "_default" and key in filename.lower():
            return gov.model_copy(deep=True)
    return GOVERNANCE["_default"].model_copy(deep=True)


def _seed_users(session: Session) -> None:
    users = UserRepository(session)
    users.upsert_group("hr_core", "인사팀 일반")
    users.upsert_group("payroll", "급여 담당")
    users.upsert_user("hr_analyst", SensitivityLevel.INTERNAL, "인사 분석가")
    users.add_user_to_group("hr_analyst", "hr_core")
    users.upsert_user("hr_lead", SensitivityLevel.RESTRICTED, "인사 팀장")
    users.add_user_to_group("hr_lead", "hr_core")
    users.add_user_to_group("hr_lead", "payroll")
    session.commit()


def _ingest_all(svc: ReviewService, samples_dir: Path) -> None:
    files = sorted(p for p in samples_dir.iterdir()
                   if p.suffix.lower() in {".txt", ".docx", ".xlsx", ".pdf", ".pptx"})
    for path in files:
        try:
            doc_id = svc.start_ingestion(str(path), ingested_by="smoke")
        except DuplicateError as e:
            print(f"  · {path.name}: 이미 적재됨({e.existing_doc_id[:8]}) — 스킵")
            continue
        gov = _governance_for(path.name)
        result = svc.submit_review(doc_id, governance=gov,
                                   lifecycle_overrides={"status": "active"})
        state = "INDEXED" if result.ok else f"BLOCKED({result.missing_fields})"
        print(f"  · {path.name}: {state} [{gov.sensitivity_level.value}/{gov.access_groups}]")


def _build_search(session: Session) -> SearchPipeline:
    embedder = TEIEmbedder()
    dense = QdrantDenseSearch(embedder=embedder)
    return SearchPipeline(
        retriever=HybridRetriever(dense=dense),   # dense-only (BM25는 하이브리드 확장 시)
        reranker=TEIReranker(),
        llm=VLLMClient(),
        audit=AuditRepository(session),
    )


def _ask(pipe: SearchPipeline, session: Session, user: UserContext, query: str) -> None:
    ans = pipe.answer(query, user, today=date.today())
    session.commit()
    print(f"\n[{user.user_id}] Q: {query}")
    print(f"  A: {ans.text}")
    if ans.citations:
        cites = "; ".join(f"[{c.marker}] {c.title} p.{c.page_no}" for c in ans.citations)
        print(f"  출처: {cites}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples-dir", default="samples")
    ap.add_argument("--query", default="연차는 며칠인가요?")
    ap.add_argument("--salary-query", default="부장 직급의 연봉 밴드는?")
    args = ap.parse_args()

    print(f"설정: vLLM={settings.vllm_base_url} embed={settings.embedding_url} "
          f"rerank={settings.reranker_url} qdrant=:{settings.qdrant_http_port}")

    engine = make_engine()
    create_all(engine)
    session = make_session_factory(engine)()

    print("\n[1] 사용자·그룹 시드")
    _seed_users(session)
    print("  hr_analyst(hr_core/INTERNAL), hr_lead(hr_core+payroll/RESTRICTED)")

    print("\n[2] 샘플 문서 적재→검토→색인")
    svc = ReviewService(session=session, llm=VLLMClient(), llm_model=settings.vllm_model,
                        embedder=TEIEmbedder(), indexer=QdrantIndexer())
    _ingest_all(svc, Path(args.samples_dir))

    print("\n[3] 질의 (접근통제 시연)")
    pipe = _build_search(session)
    analyst = UserContext("hr_analyst", frozenset(["hr_core"]), SensitivityLevel.INTERNAL)
    lead = UserContext("hr_lead", frozenset(["hr_core", "payroll"]), SensitivityLevel.RESTRICTED)

    _ask(pipe, session, analyst, args.query)
    # 급여(대외비)는 hr_analyst에겐 안 보이고, hr_lead에겐 보여야 한다
    _ask(pipe, session, analyst, args.salary_query)
    _ask(pipe, session, lead, args.salary_query)

    print("\n✅ 스모크 완료. 위 결과에서 급여 질의가 hr_analyst에겐 '확인할 수 없음',"
          " hr_lead에겐 답변+출처가 나오면 접근통제가 정상입니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
