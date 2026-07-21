"""유사(개정판 가능) 문서 탐지.

적재 시 새 문서의 대표 텍스트를 임베딩해 이미 색인된 문서 중 유사한 것을 찾는다.
접근통제 필터 없이(관리자 시점) 검색하며, 자기 자신은 제외한다.
높은 유사도만 잡히므로(낮은 유사도 개정판은 문서 관리에서 사람이 수동 연결),
결과는 검토 화면에 후보로 제시되고 최종 판단은 사람이 한다.
"""

from __future__ import annotations

from typing import Any, Protocol

# dense 코사인 유사도 임계치 (이 값 이상이면 후보)
DEFAULT_THRESHOLD = 0.88


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


def find_similar(
    client: Any, collection: str, embedder: Embedder, text: str,
    exclude_doc_id: str, top: int = 5, threshold: float = DEFAULT_THRESHOLD,
) -> list[dict[str, Any]]:
    """유사 문서 후보 [{doc_id, filename, score}] 반환(문서 단위, 자기 자신 제외)."""
    if not text.strip():
        return []
    if not client.collection_exists(collection):
        return []
    vector = embedder.embed([text[:4000]])[0]
    res = client.query_points(
        collection_name=collection, query=vector,
        limit=top * 4, with_payload=True,
    )
    best: dict[str, dict[str, Any]] = {}
    for p in res.points:
        payload = p.payload or {}
        doc_id = payload.get("parent_doc_id")
        score = float(getattr(p, "score", 0.0) or 0.0)
        if not doc_id or doc_id == exclude_doc_id or score < threshold:
            continue
        prev = best.get(doc_id)
        if prev is None or score > prev["score"]:
            best[doc_id] = {"doc_id": doc_id,
                            "filename": payload.get("source_filename"),
                            "score": round(score, 3)}
    return sorted(best.values(), key=lambda d: d["score"], reverse=True)[:top]
