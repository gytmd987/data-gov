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
  ingestion/
    intake.py            # 해시·중복탐지·시스템 자동 식별 필드
    parsers/             # 포맷별 파서(txt/docx/xlsx/pptx/pdf/image) + 레지스트리
    chunking.py          # 구조 인지 청킹(표는 통째로, 텍스트는 경계 분할+오버랩)
    enrichment.py        # LLM 자동 채움(controlled JSON 스키마 강제, confidence 임계치)
    pipeline.py          # 상태머신 오케스트레이션(자동단계→검토→검증→색인)
  search/
    access.py            # 사용자 컨텍스트 → 접근통제 하드필터(Qdrant + 파이썬 재검증)
    retriever.py         # 하이브리드 검색(dense+BM25) + RRF 융합 + 권한 후처리
    fusion.py            # Reciprocal Rank Fusion
    rerank.py            # 리랭킹(bge-reranker-v2-m3) topN→topK
    answer.py            # 근거 강제 프롬프트 + 출처 인용 파싱
    pipeline.py          # 검색→리랭킹→답변→감사로그 오케스트레이션
  clients/
    llm.py               # vLLM(OpenAI 호환, guided_json + 텍스트 생성)
    embedding.py         # TEI 임베딩(KURE-v1)
    reranker.py          # TEI 리랭커(bge-reranker-v2-m3)
    qdrant_indexer.py    # Qdrant 업서트(payload에 접근통제/생애주기 상속)
    qdrant_search.py     # Qdrant dense + BM25 검색 어댑터
  db/
    models.py            # ORM: documents/chunks/users/groups/user_groups/audit_log
    session.py           # 엔진·세션 팩토리 (Postgres 운영 / SQLite 테스트)
    mapping.py           # DocumentMetadata ↔ ORM 행 변환
    repositories.py      # Document/User/Audit 리포지토리
    persistence.py       # 파이프라인 ↔ 영속 계층 연결(중복탐지·단계 저장·컨텍스트 복원)
  review/
    service.py           # 검토 서비스(UI 비의존): 적재 시작·목록·상세·제출/검증/색인
    streamlit_app.py     # human-in-the-loop 검토 화면(얇은 UI)
    factory.py           # 실제 서비스 배선(vLLM/TEI/Qdrant/Postgres)
  eval/
    metrics.py           # Recall@k / MRR / nDCG@k (순수 함수)
    goldset.py           # 골드셋 스키마·로더(문서는 source_filename로 참조)
    judge.py             # LLM-as-judge(groundedness / relevance)
    runner.py            # 골드셋 실행 → 검색지표·접근통제 회귀·판정 집계
docs/vram.md             # VRAM 배치·튜닝 가이드
```

## 품질 평가 (골드셋 회귀 + 접근통제 보안)

```bash
python -m scripts.smoke --samples-dir samples          # 먼저 색인
python -m scripts.eval --goldset samples/goldset.json  # 검색지표 + 접근통제 회귀
python -m scripts.eval --goldset samples/goldset.json --judge   # LLM-as-judge 포함
```

- **검색 지표**: Recall@k / MRR / nDCG@k
- **접근통제 회귀(보안)**: 골드셋의 `forbidden_filenames`가 노출·인용되면 **회귀 실패(exit 1)**
- **LLM-as-judge**: groundedness / answer relevance (로컬 LLM, `--judge`)

하네스 로직은 오프라인 회귀됨(`tests/test_eval.py`) — 금지 문서 노출을 위반으로 잡는 보안 케이스 포함.

## 엔드투엔드 스모크 테스트

실제 서비스(vLLM/TEI/Qdrant/Postgres)를 띄우고 샘플 문서를 적재→검색까지 통과시켜 통합을 검증한다.
상세 절차는 [`docs/smoke_test.md`](docs/smoke_test.md).

```bash
docker compose up -d                       # Qdrant/Postgres/MinIO/TEI (vLLM은 별도 기동)
pip install -e ".[ingest,ui,postgres]"
python -m scripts.smoke --samples-dir samples
```

접근통제 시연: 급여(대외비) 질의가 `hr_analyst`에겐 "확인 불가", `hr_lead`에겐 답변+출처로 나오면 정상.

> Qdrant 색인·하드필터 배선은 실제 Qdrant 엔진(in-memory 로컬 모드)으로 회귀 테스트됨
> (`tests/test_qdrant_integration.py`) — 서버 없이도 CI에서 검증된다.

## 적재 검토 UI 실행

```bash
pip install -e ".[ingest,ui,postgres]"
streamlit run app/review/streamlit_app.py
```

사이드바에서 문서 업로드 → 자동 채움(LLM) → 화면에서 신뢰도 확인·거버넌스 필수 필드 입력 →
"검증 후 색인". 필수 필드 미충족 시 차단(BLOCKED) 사유가 표시된다. 로직은 `ReviewService`에 있어
UI 없이도 단위 테스트된다(`tests/test_review.py`).

## 적재 파이프라인 흐름

```
run_auto_stages()  : intake(해시·중복) → parse → chunk → enrich(LLM 자동채움)  → PENDING_REVIEW
apply_review()     : 사람이 거버넌스 필수 필드 입력·보정 → validate → VALIDATED | BLOCKED
index()            : 청크 임베딩 + Qdrant 업서트(접근통제 payload) → INDEXED
```

## 검색·답변 파이프라인 흐름

```
SearchPipeline.answer(query, user)
  → 하이브리드 검색(dense KURE-v1 + Qdrant BM25, 접근통제 하드필터 주입)
  → RRF 융합 + 권한 재검증(만료/대체/민감도 배제)
  → 리랭킹(bge-reranker-v2-m3) topN→topK
  → 근거 강제 답변 생성 + [n] 출처 인용 + 감사로그
```

접근통제는 이중 적용: **① Qdrant 쿼리 필터**(후보 단계 배제) + **② 파이썬 allows() 재검증**(인용 직전 방어).
외부 서비스(vLLM/TEI/Qdrant/OCR/해시조회)는 모두 Protocol/콜백으로 주입 → 서비스 없이 단위 테스트 가능.

전체 실행 계획은 설계 문서를 참고.
