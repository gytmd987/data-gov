# 엔드투엔드 스모크 테스트 런북

실제 서비스를 띄우고 샘플 문서를 적재→검색까지 한 번에 통과시켜 통합 리스크를 제거한다.

## 0. 전제

- GPU 서버(RTX 6000)에 **vLLM이 이미 Qwen3.6-27B로 서빙 중** (`.env`의 `VLLM_BASE_URL`).
- Docker / Docker Compose 사용 가능, NVIDIA Container Toolkit 설치됨(TEI가 GPU 사용).
- 폐쇄망이면 모델 가중치(KURE-v1, bge-reranker-v2-m3)를 미리 받아 `hf_cache` 볼륨에 배치하고 `.env`에 `HF_HUB_OFFLINE=1`.

## 1. 서비스 기동

```bash
cp .env.example .env         # VLLM_BASE_URL 등 확인·수정
docker compose up -d         # Qdrant / Postgres / MinIO / TEI(embed·rerank)
docker compose ps            # 모두 healthy 인지 확인
```

임베딩/리랭커 준비 확인:

```bash
curl -s localhost:8081/health && echo " embedding ok"
curl -s localhost:8082/health && echo " reranker ok"
curl -s localhost:6333/readyz && echo " qdrant ok"
# vLLM
curl -s $VLLM_BASE_URL/models | head -c 200
```

### TEI 이미지 / Blackwell 참고

- `docker-compose.yml`의 TEI 이미지 태그가 Blackwell(SM120) GPU와 호환되는지 확인한다. vLLM처럼 TEI도
  최신 GPU는 태그를 가려야 할 수 있다. 문제가 있으면 **임베딩/리랭커는 CPU로 돌려도 무방**하다(모델이
  작아 30명 규모엔 충분). CPU로 돌리려면 해당 서비스의 `deploy.resources` GPU 예약을 제거하고
  CPU 태그 이미지를 쓰면 된다.

## 2. 파이썬 환경

```bash
pip install -e ".[ingest,ui,postgres]"   # postgres = psycopg 드라이버
```

## 3. 프리플라이트 점검 (권장 — 먼저 실행)

각 서비스와 vLLM guided_json(적재 자동채움 의존) 경로를 개별 점검해 실패 지점을 특정한다.

```bash
python -m scripts.preflight
```

- ✅/❌로 Qdrant·Postgres·임베딩(차원 표시)·리랭커·vLLM 텍스트·vLLM guided_json 상태를 보여준다.
- 지난번 "LLM/임베딩 연결 오류"는 여기서 어느 서비스가 안 떴는지 바로 드러난다.
- 임베딩 차원이 1024가 아니면 경고가 뜬다 → `.env`에 `EMBEDDING_DIM`을 실제 값으로 설정.
- 모두 ✅면 스모크로 진행.

## 4. 스모크 실행

```bash
python -m scripts.smoke --samples-dir samples
```

스크립트가 수행하는 것:

1. 사용자·그룹 시드 — `hr_analyst`(hr_core / INTERNAL), `hr_lead`(hr_core+payroll / RESTRICTED)
2. `samples/` 문서 적재 → 검토 자동 승인(데모용 거버넌스 매핑) → 색인
   - `salary_bands_2026.txt` → RESTRICTED / payroll
   - 나머지 → INTERNAL / hr_core
3. 질의 시연:
   - "연차는 며칠인가요?" (hr_analyst)
   - "부장 직급의 연봉 밴드는?" (hr_analyst → **접근통제로 확인 불가**)
   - "부장 직급의 연봉 밴드는?" (hr_lead → **답변 + 출처**)

## 4. 성공 판정

- 적재 단계: 모든 문서가 `INDEXED` (필수 거버넌스가 채워졌으므로 차단 없음).
- 연차 질의: 근거 기반 답변 + `[n]` 출처가 출력.
- **접근통제 핵심**: 급여 질의가
  - `hr_analyst` → "제공된 문서에서 확인할 수 없습니다" (급여 문서가 후보에서 배제)
  - `hr_lead` → 연봉 밴드 답변 + 출처
- 감사로그: `select action, user_id, query_text from audit_log;` 로 질의 기록 확인.

## 5. 검토 UI로도 확인(선택)

```bash
streamlit run app/review/streamlit_app.py
```

문서 업로드 → 신뢰도 확인 → 거버넌스 입력 → "검증 후 색인". 필수 필드를 비우면 BLOCKED 사유가 보인다.

## 6. 정리

```bash
docker compose down          # 볼륨 유지
docker compose down -v       # 데이터까지 삭제
```

## 트러블슈팅

- **TEI가 GPU를 못 잡음**: `docker compose` 로그 확인, NVIDIA Container Toolkit / `deploy.resources.devices` 설정 점검. vLLM과 VRAM 합계가 96GB를 넘지 않는지 `nvidia-smi`.
- **Qdrant 차원 불일치**: 컬렉션 벡터 차원(기본 1024)이 KURE-v1 출력 차원과 같아야 한다. 다르면 `QdrantIndexer(vector_size=...)` 조정 후 컬렉션 재생성.
- **vLLM guided_json 오류**: 적재의 LLM 자동 채움은 `guided_json`을 사용한다. vLLM 버전에 맞는 guided decoding 백엔드(outlines 등)가 활성인지 확인.
- **답변이 비었음**: 검색 후보가 비었을 수 있음 — 색인이 됐는지(`INDEXED`), 사용자 그룹/clearance가 문서와 맞는지 확인.

> 참고: 하이브리드의 sparse(BM25) 축은 현재 dense-only로 스모크한다. BM25 활성화는 Qdrant sparse 벡터
> 구성(향후 인덱서 확장)이 필요하며, 접근통제·인용·리랭킹 등 통합 경로는 dense만으로도 전부 검증된다.
