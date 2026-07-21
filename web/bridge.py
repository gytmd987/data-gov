"""Django ↔ 도메인 서비스(SQLAlchemy) 브리지.

뷰는 요청마다 open_session()으로 세션을 열고 끝나면 close한다.
WEB_OFFLINE=1 이면 외부 서비스 없이 in-memory(offline demo) 컴포넌트로 배선된다.
"""

from __future__ import annotations

import os

_OFFLINE = os.environ.get("WEB_OFFLINE") == "1"

# 오프라인 싱글턴(요청 간 상태 유지)
_offline_engine = None
_offline_qdrant = None


def is_offline() -> bool:
    return _OFFLINE


def _offline_parts():
    global _offline_engine, _offline_qdrant
    from qdrant_client import QdrantClient
    from app.demo.offline import make_offline_engine
    if _offline_engine is None:
        _offline_engine = make_offline_engine()
        _offline_qdrant = QdrantClient(location=":memory:")
    return _offline_engine, _offline_qdrant


def open_session():
    if _OFFLINE:
        from sqlalchemy.orm import Session
        engine, _ = _offline_parts()
        return Session(engine, expire_on_commit=False)
    from app.review.factory import new_session
    return new_session()


def get_review_service(session):
    if _OFFLINE:
        from app.demo.offline import build_offline_review_service
        _, qdrant = _offline_parts()
        return build_offline_review_service(session, qdrant)
    from app.review.factory import build_service
    return build_service(session)


def get_document_manager(session):
    if _OFFLINE:
        from app.clients.qdrant_indexer import QdrantIndexer
        from app.demo.offline import EMBED_DIM
        from app.manage.service import DocumentManager
        _, qdrant = _offline_parts()
        return DocumentManager(session, indexer=QdrantIndexer(
            collection="hr_chunks", vector_size=EMBED_DIM, client=qdrant))
    from app.review.factory import build_document_manager
    return build_document_manager(session)


def get_search_pipeline(session):
    if _OFFLINE:
        from app.demo.offline import build_offline_search_pipeline
        _, qdrant = _offline_parts()
        return build_offline_search_pipeline(session, qdrant)
    from app.review.factory import build_search_pipeline
    return build_search_pipeline(session)


def get_chat_llm():
    """일반(비 RAG) 채팅용 LLM — complete_text(prompt) 프로토콜."""
    if _OFFLINE:
        from app.demo.offline import ExtractiveLLM
        return ExtractiveLLM()
    from app.clients.llm import VLLMClient
    return VLLMClient()
