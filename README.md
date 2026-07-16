# data-gov — 온프레미스 인사팀 RAG 시스템

회사 인사팀 내부 문서(docx/pptx/xlsx/pdf/jpg/txt, 한국어 위주, 민감정보 다수)를 적재하여
데이터 기반으로 질의응답하는 **온프레미스·폐쇄망** RAG 시스템.

> 설계 원칙: **적재(ingestion) 품질이 답변 품질을 결정한다.** 따라서 데이터 거버넌스를 먼저 세우고,
> LLM 자동 메타데이터 추론 + human-in-the-loop 검증 + 거버넌스 미충족 시 **적재 차단**을 파이프라인 중심에 둔다.

## 하드웨어 / 제약

- GPU: NVIDIA Blackwell RTX 6000 96GB **단일 카드** (MIG 미지원 → 프로세스별 VRAM 분할)
- 완전 온프레미스, 외부 API 호출 없음
- 사용자 30명 이하, 저동시성
- 생성 LLM은 **vLLM으로 이미 서빙 중**(Qwen3.6-27B)인 것을 전제로 함

## VRAM 배치 (중요)

vLLM(Qwen3.6-27B)을 재튜닝하여 GPU 점유를 **~70GB**로 낮춘 상태 → 잔여 **~26GB**.
자세한 배분/튜닝은 [`docs/vram.md`](docs/vram.md) 참고.

- **현재 배치**: 임베딩(BGE-M3 ~3GB) + 리랭커(bge-v2-m3 ~3GB) + 문서 파서(PaddleOCR-VL ~1-3GB) ≈ **~9GB만 사용**, 잔여 26GB 안에 여유
- **설계 원칙**: 파싱은 무거운 범용 VLM 대신 **문서 특화 파서**(소형·고정확)를 채택 → VRAM 절감 + OCR 정확도 향상. 임베딩/리랭커는 저지연 BGE 계열 유지

## 컴포넌트

| 컴포넌트 | 역할 | VRAM |
|---|---|---|
| vLLM (기구축) | 생성 LLM(Qwen3.6-27B) 서빙 | 재튜닝 권고 |
| TEI Embedding | KURE-v1 (한국어 특화, dense) + Qdrant BM25 (sparse) | ~2-3GB |
| TEI Reranker | bge-reranker-v2-m3 (한국어 최적화) | ~2-3GB |
| 문서 파서/OCR | 포맷별 파싱·OCR·표 추출 (PaddleOCR-VL 기본; MinerU2.5·dots.OCR 대안) | ~1-3GB |
| Qdrant | 청크 벡터 + payload(접근통제/생애주기) 하드 필터 | CPU/RAM |
| PostgreSQL | 메타데이터·사용자/그룹·감사로그·적재 상태 | CPU/RAM |
| MinIO | 원본 파일 보관(file_hash 중복탐지) | CPU/RAM |

## 빠른 시작

```bash
cp .env.example .env          # 환경변수 설정 (vLLM 엔드포인트 등)
docker compose up -d          # Qdrant / Postgres / MinIO / TEI(embed·rerank) 기동
pip install -e .              # 파이썬 패키지 설치
pytest                        # 거버넌스 스키마·상태머신 검증
```

> vLLM은 이 compose에 포함하지 않는다(이미 별도 기동 중). `.env`의 `VLLM_BASE_URL`로 연결한다.

## 구조

```
app/
  config.py              # 환경설정 (pydantic-settings)
  schemas/
    enums.py             # doc_type / sensitivity / status / chunk_type / pii_type ...
    metadata.py          # controlled 메타데이터 스키마 (자유 필드 금지)
    ingestion.py         # 적재 상태 머신 (UPLOADED→...→INDEXED / BLOCKED)
  governance/
    validator.py         # 거버넌스 필수 필드 검증 + 차단 로직
docs/vram.md             # VRAM 배치·튜닝 가이드
```

전체 실행 계획은 설계 문서를 참고.
