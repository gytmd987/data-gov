# VRAM 배치 가이드 (단일 96GB Blackwell RTX 6000)

## 현재 상황

vLLM(**Qwen3.6-27B**)을 재튜닝하여 GPU 점유를 **~70GB**로 낮춘 상태다. 남은 VRAM은 약 **26GB**.
(초기에는 기본값 `gpu_memory_utilization`≈0.9로 ~83GB를 선점 → 잔여 13GB였음. 재튜닝으로 여유 확보.)

RTX 6000(워크스테이션 Blackwell)은 **MIG를 지원하지 않으므로**, 한 장을 여러 프로세스가 메모리 상한으로
나눠 쓴다. 각 서비스는 자신의 VRAM 상한을 반드시 설정해 OOM을 방지한다.

**잔여 ~26GB로 가능한 배치:** 임베딩(BGE-M3 ~3GB) + 리랭커(bge-v2-m3 ~3GB) ≈ ~6GB만 상시 사용 → 여유 충분.
스캔·이미지 OCR은 **이미 뜬 Qwen3.6-27B 멀티모달을 재사용**하므로 추가 VRAM 0(기본값).

### OCR 백엔드 선택

| 백엔드 | VRAM | 특징 |
|---|---|---|
| **Qwen3.6-27B 재사용**(기본) | 0 (추가) | 이미 서빙 중인 멀티모달 LLM으로 OCR. 배포 0. 인사 문서 대부분 디지털이라 이미지 경로는 소수 |
| PaddleOCR-VL / MinerU2.5(옵션) | ~1-3GB | 표 많은 스캔본·대량 처리로 정확도·처리량이 필요할 때. 질의용 LLM과 GPU 경합 회피 |

> 기본은 재사용, 필요 시 전용 파서로 교체(설정 `ocr_backend`). 파서가 OCR 콜백 주입식이라 코드 변경 없이 전환.

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
| 스캔/이미지 OCR (Qwen3.6-27B 재사용, 기본) | 0 (추가) |
| 여유/단편화 버퍼 | 나머지 |

→ 임베딩·리랭커만 상시 GPU를 쓰고, OCR은 기존 LLM 재사용이라 추가 부담이 없다.

## 시나리오 B (현재 상태): vLLM 70GB, 잔여 ~26GB

재튜닝으로 잔여가 26GB로 늘어 경량 스택 + 적재용 VLM까지 수용 가능하다.

| 서비스 | VRAM |
|---|---|
| vLLM Qwen3.6-27B (재튜닝, 70GB) | 70GB |
| TEI Embedding (BGE-M3, 568M) | ~2-3GB |
| TEI Reranker (bge-reranker-v2-m3, 568M) | ~2-3GB |
| 스캔/이미지 OCR (Qwen3.6-27B 재사용, 기본) | 0 (추가) |
| 여유 버퍼 | ~20-22GB |

→ 임베딩/리랭커는 **작은 BGE 계열**(안전·저지연), OCR은 **기존 멀티모달 LLM 재사용**을 기본값으로.
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
