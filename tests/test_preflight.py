"""프리플라이트 점검 로직 테스트 (fake 주입, 서비스 없이)."""

from scripts.preflight import (
    CheckResult,
    check_embedding,
    check_reranker,
    check_vllm_json,
    run_checks,
)


class FakeEmbedder:
    def __init__(self, dim):
        self.dim = dim

    def embed(self, texts):
        return [[0.0] * self.dim for _ in texts]


class FakeReranker:
    def rerank(self, query, texts):
        return [0.9, 0.1][: len(texts)]


class FakeJSONLLM:
    def __init__(self, resp):
        self.resp = resp

    def complete_json(self, prompt, schema, **_):
        return self.resp


def test_check_embedding_reports_dim_and_warns():
    assert "dim=1024" in check_embedding(FakeEmbedder(1024))
    assert "⚠️" not in check_embedding(FakeEmbedder(1024))
    assert "⚠️" in check_embedding(FakeEmbedder(768))    # 차원 불일치 경고


def test_check_reranker_returns_scores():
    assert "scores=" in check_reranker(FakeReranker())


def test_check_vllm_json_ok_and_fail():
    assert "OK" in check_vllm_json(FakeJSONLLM({"language": "ko"}))
    # guided_json이 스키마를 안 지키면 실패로 잡힘
    import pytest
    with pytest.raises(ValueError):
        check_vllm_json(FakeJSONLLM({"lang": "ko"}))


def test_run_checks_captures_failures():
    def good():
        return "OK"

    def bad():
        raise ConnectionError("연결 거부")

    results = run_checks([("A", good), ("B", bad)])
    assert results[0] == CheckResult("A", True, "OK")
    assert results[1].ok is False
    assert "ConnectionError" in results[1].detail
