"""실제 서비스(vLLM/TEI/Qdrant/Postgres)로 ReviewService를 구성하는 팩토리.

Streamlit 앱이 사용한다. 테스트는 fake를 직접 주입하므로 이 팩토리를 쓰지 않는다.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.clients.embedding import TEIEmbedder
from app.clients.llm import VLLMClient
from app.clients.qdrant_indexer import QdrantIndexer
from app.clients.vision import VLLMVisionOCR
from app.config import settings
from app.db.session import create_all, make_engine, make_session_factory
from app.review.service import ReviewService


def build_ocr():
    """설정에 따른 OCR 콜백. 기본은 이미 뜬 Qwen3.6-27B 멀티모달 재사용."""
    backend = settings.ocr_backend.lower()
    if backend == "vllm":
        return VLLMVisionOCR()
    if backend == "none":
        return None
    # "paddleocr-vl" 등 전용 파서는 배포 후 여기에 어댑터 연결(TODO)
    return VLLMVisionOCR()

_engine = None
_SessionFactory = None


def _session_factory():
    global _engine, _SessionFactory
    if _SessionFactory is None:
        _engine = make_engine()
        create_all(_engine)
        _SessionFactory = make_session_factory(_engine)
    return _SessionFactory


def build_service(session: Session | None = None) -> ReviewService:
    session = session or _session_factory()()
    return ReviewService(
        session=session,
        llm=VLLMClient(),
        llm_model=settings.vllm_model,
        embedder=TEIEmbedder(),
        indexer=QdrantIndexer(),
        ocr=build_ocr(),   # 기본: Qwen3.6-27B 멀티모달 OCR (설정 ocr_backend)
    )
