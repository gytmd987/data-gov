# VRAM 배치 가이드 (단일 96GB Blackwell RTX 6000)

## 현재 상황

vLLM(**Qwen3.6-27B**)을 재튜닝하여 GPU 점유를 **~70GB**로 낮춘 상태다. 남은 VRAM은 약 **26GB**.
(초기에는 기본값 `gpu_memory_utilization`≈0.9로 ~83GB를 선점 → 잔여 13GB였음. 재튜닝으로 여유 확보.)

RTX 6000(워크스테이션 Blackwell)은 **MIG를 지원하지 않으므로**, 한 장을 여러 프로세스가 메모리 상한으로
나눠 쓴다. 각 서비스는 자신의 VRAM 상한을 반드시 설정해 OOM을 방지한다.

**잔여 ~26GB로 가능한 배치:** 임베딩(BGE-M3 ~3GB) + 리랭커(bge-v2-m3 ~3GB) + **적재용 Qwen3-VL-8B(~18GB)까지 on-demand 수용 가능**(합계 ~24GB).
필요하면 vLLM을 조금 더 낮춰(시나리오 A) 상시 여유를 더 확보할 수 있다.

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
| VLM (Qwen3-VL-8B, 적재 전용·선택, on-demand) | ~18GB |
| 여유/단편화 버퍼 | 나머지 |

→ 임베딩·리랭커 상시 구동 + 적재 시 무거운 VLM까지 여유롭게 수용.

## 시나리오 B (현재 상태): vLLM 70GB, 잔여 ~26GB

재튜닝으로 잔여가 26GB로 늘어 경량 스택 + 적재용 VLM까지 수용 가능하다.

| 서비스 | VRAM |
|---|---|
| vLLM Qwen3.6-27B (재튜닝, 70GB) | 70GB |
| TEI Embedding (BGE-M3, 568M) | ~2-3GB |
| TEI Reranker (bge-reranker-v2-m3, 568M) | ~2-3GB |
| 파싱 VLM: Qwen3-VL-8B (적재 전용, on-demand) 또는 Granite-Docling-258M | ~18GB / ~1-2GB |
| 여유 버퍼 | ~2-5GB |

→ 임베딩/리랭커는 여전히 **작은 BGE 계열**을 기본값으로(안전·저지연). 파싱은 이제 무거운 **Qwen3-VL-8B를 적재 시점에만 on-demand**로 쓸 수 있다.
질의 트래픽과 겹쳐 VRAM이 빠듯하면 파싱 VLM을 Granite-Docling(258M) 또는 **CPU**로 폴백한다.

> 참고: 임베딩·리랭커가 상시 ~6GB를 쓰므로, 적재 VLM(18GB)을 동시에 올리면 잔여 여유가 ~2GB로 얇아진다.
> 적재와 질의 피크가 겹치는 운영이면 vLLM을 시나리오 A(~46GB)로 더 낮춰 여유를 키우는 것을 권장.

## 권장 결론

- **가능하면 시나리오 A로 재튜닝**한다(품질·확장 여유가 크게 늘어난다).
- 코드/구성은 **시나리오 B에서도 그대로 동작**하도록 경량 모델을 기본값으로 채택한다.
  (`.env`의 `EMBEDDING_MODEL`, `RERANKER_MODEL`, `PARSER_VLM` 로 교체 가능)

## VRAM 상한 설정 위치

- vLLM: `--gpu-memory-utilization`, `--max-model-len`, `--max-num-seqs`
- TEI(임베딩/리랭커): `docker-compose.yml`의 `NVIDIA_VISIBLE_DEVICES` + `--cuda-memory-fraction`(지원 버전) 또는 프로세스별 상한
- PaddleOCR / Docling VLM: `CUDA_VISIBLE_DEVICES` 및 필요 시 CPU 폴백(`PARSER_DEVICE=cpu`)

측정: `watch -n1 nvidia-smi` 로 총 사용량이 96GB를 넘지 않는지 상시 확인한다.
