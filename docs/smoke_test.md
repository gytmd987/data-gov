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

### TEI 이미지 / Blackwell 참고 (GPU 예약 실패 시)

증상: `docker compose ps`에 컨테이너가 **3개(postgres/qdrant/minio)만** 뜨고 embedding·reranker는
로그도 비어 있음 → GPU 예약(`could not select device driver "nvidia" with capabilities: [[gpu]]`)을
못 잡아 두 서비스가 생성조차 안 된 상태다. NVIDIA Container Toolkit 미설치 또는 Blackwell(SM120)
이미지 비호환이 원인.

해결: **임베딩·리랭커를 CPU로** 돌린다(568M 소형이라 30명 규모엔 충분). 준비된 CPU 전용 파일 사용:

```bash
docker compose -f docker-compose.cpu.yml up -d
docker compose -f docker-compose.cpu.yml ps            # embedding/reranker 가 Up 인지
docker compose -f docker-compose.cpu.yml logs embedding --tail=20   # "Ready" 확인
```

> 이후 명령에도 같은 `-f docker-compose.cpu.yml`을 붙인다(ps/logs/down 등).
> vLLM은 여전히 별도 GPU 서버에서 기동돼 있어야 한다(이 compose에 없음).

#### GPU로 돌리고 싶다면 (NVIDIA Container Toolkit 설치)

`could not select device driver "nvidia"`는 드라이버가 아니라 **컨테이너 툴킷 미설치**가 원인이다.

```bash
# 0) 호스트에 드라이버가 있는지 먼저 확인
nvidia-smi                       # 여기서 GPU가 보여야 함(안 보이면 드라이버부터)

# 1) NVIDIA Container Toolkit 설치 (Ubuntu/Debian, 인터넷 필요)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit

# 2) 도커 런타임에 등록 + 재시작
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# 3) 검증
docker info | grep -i nvidia                    # Runtimes 에 nvidia 보이면 OK
docker run --rm --gpus all ubuntu nvidia-smi    # (이미지 접근 가능할 때)

# 4) GPU 스택으로 기동
docker compose up -d              # docker-compose.yml (embedding·reranker GPU)
```

폐쇄망이면 `nvidia.github.io` 접근이 막힐 수 있다 → 사내 미러 사용하거나 인프라팀에 툴킷 설치 요청.
Blackwell(SM120)은 최신 드라이버 + 최신 툴킷이 필요하다. **설치가 번거로우면 CPU(위)가 30명 규모엔 충분하다.**

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
