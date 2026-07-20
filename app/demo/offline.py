"""오프라인 데모용 결정적 컴포넌트 (외부 서비스 0개).

실제 vLLM/TEI/Qdrant/Postgres 없이 전체 파이프라인(적재→질의→접근통제→인용)을
로컬에서 시연·테스트하기 위한 fake 구현.
- HashingEmbedder: 한국어 문자 bigram 해싱 TF 벡터(어휘 겹침 = 코사인 유사).
- OverlapReranker: 질의-청크 bigram 겹침 점수.
- ExtractiveLLM: 답변은 근거 첫 블록 추출 + [1] 인용, 자동채움은 최소 유효 스키마.
- Qdrant는 QdrantClient(location=":memory:"), DB는 SQLite in-memory(StaticPool).
"""

from __future__ import annotations

import math
import re
from typing import Any

from qdrant_client import QdrantClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.clients.qdrant_indexer import QdrantIndexer
from app.clients.qdrant_search import QdrantDenseSearch
from app.db.repositories import AuditRepository, UserRepository
from app.db.session import create_all
from app.review.service import ReviewService
from app.search.pipeline import SearchPipeline
from app.search.retriever import HybridRetriever

EMBED_DIM = 512


def _bigrams(text: str) -> list[str]:
    # 공백/문장부호 제거 후 문자 bigram (한국어 조사 변형에 강함)
    s = re.sub(r"[\s\W_]+", "", text)
    if len(s) < 2:
        return [s] if s else []
    return [s[i:i + 2] for i in range(len(s) - 1)]


class HashingEmbedder:
    """bigram 해싱 TF 벡터(정규화). 어휘가 겹칠수록 코사인 유사도가 높다."""

    def __init__(self, dim: int = EMBED_DIM) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for bg in _bigrams(t):
                v[hash(bg) % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out


class OverlapReranker:
    def rerank(self, query: str, texts: list[str]) -> list[float]:
        q = set(_bigrams(query))
        if not q:
            return [0.0] * len(texts)
        return [len(q & set(_bigrams(t))) / len(q) for t in texts]


class ExtractiveLLM:
    """근거 첫 블록을 추출해 답변(+[1]). 자동채움은 최소 유효 스키마 반환."""

    # 줄 시작의 근거 헤더 "[1]"만 매칭(시스템 프롬프트 예시의 [1] 회피)
    _EVIDENCE = re.compile(r"^\[1\][^\n]*\n(.+?)(?=\n\n|\Z)", re.DOTALL | re.MULTILINE)
    _QUESTION = re.compile(r"\[질문\]\n(.+?)\n", re.DOTALL)
    _RELEVANCE_FLOOR = 0.12

    def complete_text(self, prompt: str, temperature: float = 0.0) -> str:
        m = self._EVIDENCE.search(prompt)
        if not m:
            return "제공된 문서에서 확인할 수 없습니다."
        snippet = " ".join(m.group(1).split())[:200]
        # 근거가 질문과 실제로 관련 있을 때만 답한다(무관한 허용문서 인용 방지).
        qm = self._QUESTION.search(prompt)
        if qm:
            q = set(_bigrams(qm.group(1)))
            ev = set(_bigrams(snippet))
            if q and len(q & ev) / len(q) < self._RELEVANCE_FLOOR:
                return "제공된 문서에서 확인할 수 없습니다."
        return f"{snippet} [1]"

    _FILENAME = re.compile(r"파일명:\s*(.+)")

    def complete_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        props = schema.get("properties", {})
        # LLM-as-judge 스키마면 판정 점수를 반환(데모용 휴리스틱)
        if "groundedness" in props:
            denied = "확인할 수 없습니다" in prompt
            return {"groundedness": 1.0, "relevance": 0.3 if denied else 0.9}
        doc_type = "payroll" if "급여" in prompt or "연봉" in prompt else "policy"
        result = {
            "doc_type": {"value": doc_type, "confidence": 0.9},
            "language": {"value": "ko", "confidence": 0.99},
            "status": {"value": "active", "confidence": 0.9},
        }
        fm = self._FILENAME.search(prompt)
        if fm:
            title = fm.group(1).strip().rsplit(".", 1)[0]
            result["title_normalized"] = {"value": title, "confidence": 0.9}
        return result


def make_offline_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool, future=True)
    create_all(engine)
    return engine


def build_offline_review_service(session: Session, qdrant: QdrantClient) -> ReviewService:
    return ReviewService(
        session=session,
        llm=ExtractiveLLM(), llm_model="offline",
        embedder=HashingEmbedder(),
        indexer=QdrantIndexer(collection="hr_chunks", vector_size=EMBED_DIM, client=qdrant),
    )


def build_offline_search_pipeline(session: Session, qdrant: QdrantClient) -> SearchPipeline:
    dense = QdrantDenseSearch(embedder=HashingEmbedder(), collection="hr_chunks", client=qdrant)
    return SearchPipeline(
        retriever=HybridRetriever(dense=dense),
        reranker=OverlapReranker(),
        llm=ExtractiveLLM(),
        audit=AuditRepository(session),
        top_k=5,
    )
