"""TEI 배치 분할 테스트 — 배치 경계에서 index 매핑이 맞는지(서버 없이 httpx 모킹)."""

from app.clients import reranker as reranker_mod


class _Resp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """batch 내 상대 index로 응답. score=텍스트에 박힌 전역 id → 매핑 검증용."""

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json):
        texts = json["texts"]
        assert len(texts) <= 32                      # 배치 상한 준수
        return _Resp([{"index": i, "score": float(t)} for i, t in enumerate(texts)])


def test_rerank_batches_and_maps_indices(monkeypatch):
    monkeypatch.setattr(reranker_mod.httpx, "Client", _FakeClient)
    texts = [str(i) for i in range(40)]              # 40개 → 32 + 8 두 배치
    scores = reranker_mod.TEIReranker().rerank("q", texts)
    assert scores == [float(i) for i in range(40)]   # 전역 index로 정확히 복원
