"""업로드가 안 될 때 — 필요한 서비스가 다 떠 있는지 한 번에 확인한다.

    python -m scripts.check_services

문서 하나를 올리면 뒤에서 이만큼이 순서대로 돈다. 하나라도 빠지면 업로드가 실패한다.

    파일 저장(디스크) → 파싱 → AI 자동채움(vLLM) → 임베딩(TEI) → 색인(Qdrant) → DB

화면에는 "처리 중 오류"만 뜨므로, 어디가 끊겼는지는 여기서 본다.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import httpx

from app.config import settings
from app.ingestion.failures import explain

MIN_FREE_MB = 500          # 이보다 적으면 업로드가 곧 깨진다


def _check(label: str, fn, where: str = "") -> bool:
    try:
        detail = fn()
    except Exception as e:                      # noqa: BLE001
        print(f"  ❌ {label}")
        print(f"       {explain(e, where)}")
        print(f"       ({type(e).__name__}: {str(e)[:120]})")
        return False
    print(f"  ✅ {label}{f' — {detail}' if detail else ''}")
    return True


def _http_ok(url: str, path: str = "/health") -> str:
    resp = httpx.get(url.rstrip("/") + path, timeout=10)
    resp.raise_for_status()
    return "응답 정상"


def _vllm() -> str:
    url = settings.vllm_base_url.rstrip("/") + "/models"
    resp = httpx.get(url, headers={"Authorization": f"Bearer {settings.vllm_api_key}"},
                     timeout=15)
    resp.raise_for_status()
    served = [m.get("id") for m in resp.json().get("data", [])]
    if settings.vllm_model not in served:
        raise RuntimeError(
            f"vLLM 404 @ {url}: 설정된 모델 '{settings.vllm_model}' 이 없습니다. "
            f"서빙 중: {served}")
    return f"모델 '{settings.vllm_model}'"


def _qdrant() -> str:
    from qdrant_client import QdrantClient
    client = QdrantClient(host="localhost", port=settings.qdrant_http_port)
    names = [c.name for c in client.get_collections().collections]
    return f"컬렉션 {len(names)}개"


def _database() -> str:
    from sqlalchemy import text as sql
    from app.review.factory import new_session
    session = new_session()
    try:
        session.execute(sql("SELECT 1"))
        return "연결 정상"
    finally:
        session.close()


def _disk() -> str:
    """업로드는 임시 파일 → 보관 폴더 순으로 디스크를 쓴다. 꽉 차면 조용히 실패한다."""
    import tempfile
    worst = None
    for label, path in (("임시", tempfile.gettempdir()),
                        ("보관", settings.storage_dir),
                        ("대기열", settings.upload_queue_dir)):
        target = Path(path)
        while not target.exists() and target != target.parent:
            target = target.parent
        free_mb = shutil.disk_usage(target).free / 1024 / 1024
        if worst is None or free_mb < worst[1]:
            worst = (label, free_mb, path)
    label, free_mb, path = worst
    if free_mb < MIN_FREE_MB:
        raise OSError(28, f"여유 {free_mb:.0f}MB ({label}: {path})")
    return f"여유 {free_mb / 1024:.1f}GB ({label} 기준)"


def main(argv=None) -> int:
    print("문서 업로드에 필요한 것들을 확인합니다.\n")
    print("── 저장소 ──────────────────────────────────────────────")
    ok = [_check("디스크 여유", _disk),
          _check("데이터베이스(PostgreSQL)", _database, "postgres"),
          _check("검색 저장소(Qdrant)", _qdrant, "qdrant")]

    print("\n── AI 서비스 ───────────────────────────────────────────")
    ok += [_check("vLLM(요약·분류)", _vllm, settings.vllm_base_url),
           _check("TEI 임베딩", lambda: _http_ok(settings.embedding_url),
                  settings.embedding_url),
           _check("TEI 리랭커" + ("" if settings.rerank_enabled else " (꺼짐)"),
                  lambda: _http_ok(settings.reranker_url)
                  if settings.rerank_enabled else "설정에서 꺼 둠",
                  settings.reranker_url)]

    print("\n── 판정 ────────────────────────────────────────────────")
    if all(ok):
        print("  ✅ 전부 정상입니다.")
        print("     그래도 업로드가 실패하면 특정 파일 문제일 수 있습니다:")
        print("       journalctl -u hr-web -n 100    # 실패한 파일명과 자세한 오류")
        return 0
    print(f"  ❌ {ok.count(False)}건이 문제입니다. 위의 ❌ 부터 고치세요.")
    print("     도커로 띄우는 것들: docker compose ps · docker compose up -d")
    print("     자세한 오류:       journalctl -u hr-web -n 100")
    return 1


if __name__ == "__main__":
    sys.exit(main())
