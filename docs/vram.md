# VRAM 배치 가이드 (단일 96GB Blackwell RTX 6000)

## 현재 상황

vLLM(**Qwen3.6-27B**)을 재튜닝하여 GPU 점유를 **~70GB**로 낮춘 상태다. 남은 VRAM은 약 **26GB**.
(초기에는 기본값 `gpu_memory_utilization`≈0.9로 ~83GB를 선점 → 잔여 13GB였음. 재튜닝으로 여유 확보.)

RTX 6000(워크스테이션 Blackwell)은 **MIG를 지원하지 않으므로**, 한 장을 여러 프로세스가 메모리 상한으로
나눠 쓴다. 각 서비스는 자신의 VRAM 상한을 반드시 설정해 OOM을 방지한다.

**잔여 ~26GB로 가능한 배치:** 임베딩(BGE-M3 ~3GB) + 리랭커(bge-v2-m3 ~3GB) + **문서 파서(PaddleOCR-VL ~1-3GB)** ≈ ~9GB만 사용 → 여유 충분.
(범용 Qwen3-VL-8B ~18GB 대신 문서 특화 파서를 쓰면 VRAM도 절약되고 문서 OCR 정확도도 더 높다 — 아래 참고.)

### 파싱 VLM 선택 (문서 특화 > 범용)

OmniDocBench 등 문서 파싱 벤치에서 범용 Qwen3-VL은 하위권, 문서 특화 모델이 소형·고정확이다:

| 모델 | 크기 | 특징 |
|---|---|---|
| **PaddleOCR-VL**(기본) | 소형 | 표·정형문서 강점, 100+ 언어(한국어 포함), OmniDocBench 최상위권 |
| MinerU2.5-Pro | 1.2B | 초경량 SOTA 문서 파싱 |
| dots.OCR | 소형 | 실측 문자 정확도 1위, 레이아웃 인식 |

> 생성 LLM **Qwen3.6-27B가 네이티브 멀티모달**이라, 답변 시점의 가벼운 이미지 이해는 별도 VLM 없이 처리 가능하다.
> 무거운 범용 VLM 상주는 불필요.

## 시나리오 A (권고): vLLM 재튜닝으로 여유 확보

30명·저동시성 환경에서는 거대한 KV 캐시가 불필요하다. vLLM을 다음과 같이 캡한다:

```bash
vllm serve <qwen3.6-27b> \
  --quantization fp8 \            # Blackwell 5세대 텐서코어 네이티브 FP8
  --gpu-memory-utilization 0.48 \ # GPU의 약 46GB로 상한
  --max-model-len 16384 \         # 필요 이상으로 긴 컨텍스트면 KV 캐시 축소
  --max-num-seqs 16               # 동시 시퀀스 상한(저동시성이면 충분)
```

| 서비스 | VRAM |
|---|---|
| vLLM Qwen3.6-27B (FP8, util≈0.48) | ~43-48GB |
| TEI Embedding (BGE-M3, 568M, fp16) | ~2-3GB |
| TEI Reranker (bge-reranker-v2-m3, 568M, fp16) | ~2-3GB |
| 문서 파서 (PaddleOCR-VL, 적재 전용) | ~1-3GB |
| 여유/단편화 버퍼 | 나머지 |

→ 임베딩·리랭커·문서 파서까지 상시 구동해도 여유가 매우 크다(범용 VLM 불필요).

## 시나리오 B (현재 상태): vLLM 70GB, 잔여 ~26GB

재튜닝으로 잔여가 26GB로 늘어 경량 스택 + 적재용 VLM까지 수용 가능하다.

| 서비스 | VRAM |
|---|---|
| vLLM Qwen3.6-27B (재튜닝, 70GB) | 70GB |
| TEI Embedding (BGE-M3, 568M) | ~2-3GB |
| TEI Reranker (bge-reranker-v2-m3, 568M) | ~2-3GB |
| 문서 파서: PaddleOCR-VL (적재 전용) | ~1-3GB |
| 여유 버퍼 | ~14-18GB |

→ 임베딩/리랭커는 **작은 BGE 계열**(안전·저지연), 파싱은 **문서 특화 소형 파서**를 기본값으로.
상시 사용량이 ~9GB에 그쳐 잔여 26GB 안에서 넉넉하다. 피크가 겹쳐도 여유가 크다.

## 권장 결론

- **가능하면 시나리오 A로 재튜닝**한다(품질·확장 여유가 크게 늘어난다).
- 코드/구성은 **시나리오 B에서도 그대로 동작**하도록 경량 모델을 기본값으로 채택한다.
  (`.env`의 `EMBEDDING_MODEL`, `RERANKER_MODEL`, `PARSER_VLM` 로 교체 가능)

## VRAM 상한 설정 위치

- vLLM: `--gpu-memory-utilization`, `--max-model-len`, `--max-num-seqs`
- TEI(임베딩/리랭커): `docker-compose.yml`의 `NVIDIA_VISIBLE_DEVICES` + `--cuda-memory-fraction`(지원 버전) 또는 프로세스별 상한
- PaddleOCR / Docling VLM: `CUDA_VISIBLE_DEVICES` 및 필요 시 CPU 폴백(`PARSER_DEVICE=cpu`)

측정: `watch -n1 nvidia-smi` 로 총 사용량이 96GB를 넘지 않는지 상시 확인한다.
