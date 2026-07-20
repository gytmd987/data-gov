"""실서비스 프리플라이트 점검.

스모크 전에 각 서비스(Qdrant/Postgres/TEI 임베딩·리랭커/vLLM 텍스트·JSON)를 개별 점검해
어디가 막혔는지 콕 집어준다. 하나라도 실패하면 종료 코드 1.

    python -m scripts.preflight
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Callable


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _run(name: str, fn: Callable[[], str]) -> CheckResult:
    try:
        return CheckResult(name, True, fn())
    except Exception as e:  # noqa: BLE001 - 진단 목적상 모든 예외 표시
        return CheckResult(name, False, f"{type(e).__name__}: {e}")


# ── 개별 점검(주입식이라 테스트 가능) ────────────────────────────────────────
def check_qdrant(client) -> str:
    cols = client.get_collections()
    return f"OK (collections={len(cols.collections)})"


def check_postgres(engine) -> str:
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return "OK"


def check_embedding(embedder) -> str:
    vec = embedder.embed(["연차 테스트"])[0]
    dim = len(vec)
    warn = "" if dim == 1024 else f"  ⚠️ 차원 {dim}≠1024 → EMBEDDING_DIM 설정 필요"
    return f"OK (dim={dim}){warn}"


def check_reranker(reranker) -> str:
    scores = reranker.rerank("연차 며칠?", ["연차는 15일", "무관한 문장"])
    return f"OK (scores={[round(s, 3) for s in scores]})"


def check_vllm_chat(llm) -> str:
    text = llm.complete_text("한 단어로만 답하라: 대한민국의 수도는?")
    return f"OK ('{text.strip()[:40]}')"


def check_vllm_json(llm) -> str:
    """적재 자동채움이 의존하는 guided_json 경로 검증."""
    schema = {
        "type": "object",
        "properties": {"language": {"type": "string", "enum": ["ko", "en"]}},
        "required": ["language"],
    }
    out = llm.complete_json("이 문서의 언어 코드를 JSON으로 답하라. 한국어 문서다.", schema)
    if "language" not in out:
        raise ValueError(f"guided_json 응답에 language 없음: {out}")
    return f"OK ({out})"


def run_checks(checks: list[tuple[str, Callable[[], str]]]) -> list[CheckResult]:
    return [_run(name, fn) for name, fn in checks]


def main() -> int:
    from app.clients.embedding import TEIEmbedder
    from app.clients.llm import VLLMClient
    from app.clients.reranker import TEIReranker
    from app.config import settings
    from app.db.session import make_engine
    from qdrant_client import QdrantClient

    print(f"대상: vLLM={settings.vllm_base_url} embed={settings.embedding_url} "
          f"rerank={settings.reranker_url} qdrant=:{settings.qdrant_http_port} "
          f"pg=:{settings.postgres_port}")

    llm = VLLMClient()
    checks = [
        ("Qdrant", lambda: check_qdrant(QdrantClient(
            host="localhost", port=settings.qdrant_http_port))),
        ("Postgres", lambda: check_postgres(make_engine())),
        ("TEI 임베딩(KURE-v1)", lambda: check_embedding(TEIEmbedder())),
        ("TEI 리랭커(bge-v2-m3)", lambda: check_reranker(TEIReranker())),
        ("vLLM 텍스트 생성", lambda: check_vllm_chat(llm)),
        ("vLLM guided_json(적재 자동채움)", lambda: check_vllm_json(llm)),
    ]
    results = run_checks(checks)

    print()
    for r in results:
        mark = "✅" if r.ok else "❌"
        print(f"  {mark} {r.name}: {r.detail}")

    failed = [r for r in results if not r.ok]
    if failed:
        print(f"\n{len(failed)}개 실패 — 위 오류를 해결한 뒤 스모크를 실행하세요.")
        return 1
    print("\n모든 서비스 정상. `python -m scripts.smoke --samples-dir samples` 진행 가능.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
