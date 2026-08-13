"""임베딩·리랭커(TEI)가 GPU 로 도는지 확인하고, 실제로 얼마나 걸리는지 재 본다.

    python -m scripts.check_tei

리랭커는 **크로스 인코더**라 후보마다 모델을 한 번씩 돌린다. GPU 면 후보 24개에
0.2~0.5초지만 CPU 면 8초가 넘는다 — 질문 하나가 그만큼 느려진다는 뜻이다.
그런데 CPU 로 떨어져도 **에러 없이 조용히 도니까** 아무도 모른다. 그래서 잰다.
"""

from __future__ import annotations

import sys
import time

import httpx

from app.config import settings

# 이 정도면 GPU 는 확실히 여유롭고, CPU 는 확실히 티가 난다
PROBE_TEXTS = 24
PROBE_CHARS = 1000
GPU_LIKELY_SEC = 1.5      # 이보다 빠르면 GPU 로 본다


def _info(base: str) -> dict:
    try:
        return httpx.get(f"{base.rstrip('/')}/info", timeout=10).json()
    except Exception as e:      # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


def _time_rerank(base: str) -> float | None:
    texts = ["연차 휴가 규정에 따른 산정 방식과 신청 절차. " * 20] * PROBE_TEXTS
    body = {"query": "연차는 며칠인가요?",
            "texts": [t[:PROBE_CHARS] for t in texts], "truncate": True}
    began = time.monotonic()
    try:
        resp = httpx.post(f"{base.rstrip('/')}/rerank", json=body, timeout=120)
        resp.raise_for_status()
    except Exception as e:      # noqa: BLE001
        print(f"  ❌ 리랭킹 요청 실패: {type(e).__name__}: {e}")
        return None
    return time.monotonic() - began


def main(argv=None) -> int:
    print("── TEI 상태 ────────────────────────────────────────────")
    for label, base in (("임베딩", settings.embedding_url),
                        ("리랭커", settings.reranker_url)):
        info = _info(base)
        if "_error" in info:
            print(f"  ❌ {label}: 붙지 못했습니다 — {info['_error']} ({base})")
            continue
        print(f"  {label}: {info.get('model_id', '?')} · {base}")
        # TEI 는 버전에 따라 이 필드가 없을 수 있어 있으면만 보여준다
        for key in ("model_dtype", "max_batch_tokens", "max_input_length"):
            if key in info:
                print(f"      {key}: {info[key]}")

    print("\n── 리랭킹 실측 ─────────────────────────────────────────")
    if not settings.rerank_enabled:
        print("  리랭커가 설정에서 꺼져 있습니다(RERANK_ENABLED=false).")
        print("  검색 융합 순위를 그대로 씁니다 — 재 볼 것이 없습니다.")
        return 0

    took = _time_rerank(settings.reranker_url)
    if took is None:
        return 1
    print(f"  후보 {PROBE_TEXTS}개 × {PROBE_CHARS}자 → {took:.2f}초")

    print("\n── 판정 ────────────────────────────────────────────────")
    if took <= GPU_LIKELY_SEC:
        print("  ✅ GPU 로 도는 것으로 보입니다. 이 정도면 정상입니다.")
        return 0

    print(f"  ❌ **CPU 로 도는 것으로 보입니다**({took:.1f}초). "
          "GPU 면 0.2~0.5초입니다.")
    print("     질문 하나마다 이 시간이 그대로 붙습니다.\n")
    print("  고치는 순서:")
    print("   1) 컨테이너가 GPU 를 받았는지")
    print("        docker compose logs reranker | head -30")
    print("        (Blackwell/RTX 6000 이면 `cuda compute cap 120 is not supported`"
          " 류가 보일 수 있습니다)")
    print("   2) 그 메시지가 보이면 **TEI 이미지가 이 GPU 를 지원 안 하는 것**입니다.")
    print("        docs/smoke_test.md 의 'Blackwell 이미지' 절대로 직접 빌드하세요")
    print("        (CUDA_COMPUTE_CAP=120).")
    print("   3) `could not select device driver \"nvidia\"` 면 컨테이너 툴킷 문제입니다")
    print("        docs/smoke_test.md 56~68행.")
    print("\n  GPU 를 못 쓰는 상황이면 — **끄기 전에 줄여 보세요.**")
    print("     비용은 (후보 수 × 글자 수)에 거의 비례하므로 둘 다 줄이면 곱으로 줄어듭니다.")
    print(f"       RERANK_TOP_N=8        # 지금 {settings.rerank_top_n}")
    print(f"       RERANK_MAX_CHARS=600  # 지금 {settings.rerank_max_chars}")
    print(f"     → 대략 {took:.0f}초 → {took / 6:.1f}초.")
    print("     품질 손실은 생각보다 작습니다. 하이브리드 검색(의미+BM25) 융합이 이미")
    print("     쓸 만한 순위를 만들어 두고, 리랭커는 그 상위 몇 개를 다시 줄 세울 뿐입니다.")
    print("\n  그래도 느리면 끄세요 — 0초가 됩니다:")
    print("     RERANK_ENABLED=false   (검색 융합 순위를 그대로 씁니다)")
    print("     끈 뒤 품질이 실제로 떨어지는지는 app/eval/ 골든셋으로 재 보세요.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
