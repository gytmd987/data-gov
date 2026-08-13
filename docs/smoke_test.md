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

**일단 띄우려면** 임베딩·리랭커를 CPU 로 돌린다. 준비된 CPU 전용 파일이 있다:

> ⚠️ **CPU 는 "일단 동작하게" 하는 임시 방편이지 운영용이 아니다.** 실측으로
> 리랭킹 8초, 문서 1건 색인에 CPU 전부 점유다(아래 실측표). 반드시 아래 설정을 함께
> 조정하고, GPU 를 쓸 수 있게 되면 되돌릴 것.
>
> ```ini
> RERANK_TOP_N=8 · RERANK_MAX_CHARS=600 · INGEST_WORKERS=1
> ```


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

#### 그런데 GPU 예약은 되는데 TEI 컨테이너가 CUDA 에러로 죽는다면 (Blackwell 이미지)

`:latest` TEI 이미지가 SM120을 지원 안 하는 버전일 수 있다(로그에 `cuda compute cap 120 is not supported` 류).
TEI는 최근 릴리스에서 Blackwell을 지원하므로 **더 최신 태그**로 바꾼다 — compose 수정 없이 `.env`로:

```bash
# .env  (릴리스 페이지에서 최신 버전 확인 후 지정)
TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:1.8
```

로그에 `runtime compute cap 120 is not compatible with compile time compute cap 80` 이 뜨면
= 그 이미지가 SM80(A100)용으로 빌드된 것. Blackwell(120)용 이미지가 필요하다.

**폐쇄망이면 인터넷 되는 머신에서 빌드 → 이미지를 서버로 반입**한다.

> `Dockerfile-cuda` 는 **TEI 소스 저장소 안에 있는 파일**이다. 우리 프로젝트 폴더에서
> `docker build -f Dockerfile-cuda …` 를 치면 당연히 그런 파일이 없다고 나온다.
> 반드시 아래처럼 **먼저 clone 하고 그 폴더로 들어가서** 빌드해야 한다.

```bash
# (인터넷 되는 머신에서)
git clone https://github.com/huggingface/text-embeddings-inference
cd text-embeddings-inference        # ← 이 폴더 안에 Dockerfile-cuda 가 있다
docker build -f Dockerfile-cuda --build-arg CUDA_COMPUTE_CAP=120 -t tei-blackwell:local .
docker save tei-blackwell:local -o tei-blackwell.tar

# (서버로 전송 후)
docker load -i tei-blackwell.tar
# → .env 에  TEI_IMAGE=tei-blackwell:local
```

> 요구: NVIDIA 드라이버가 CUDA 12.2+ 호환이어야 한다(Blackwell이면 최신 드라이버라 보통 충족).

#### ⚠️ "CPU 로도 충분하다"는 말은 사실이 아니다 (실측)

이 문서에 예전에 "임베딩/리랭커는 소형이라 CPU 지연도 문제되지 않는다"고 적혀 있었다.
**실제로 재 보니 틀렸다.** 모델이 작은 건 맞지만(568M) 처리량이 크다.

| 작업 | 통과 토큰 | CPU | GPU(추정) |
|---|---|---|---|
| 채팅 질문 임베딩 | ~15 | 0.1초 | 0.05초 |
| 리랭킹(후보 24개) | ~17,000 | **8초** | 0.3초 |
| 문서 1건 색인(50청크) | ~30,000 | **~15초, CPU 전부 점유** | 0.3초 |

질문 임베딩만 보면 빨라서 괜찮아 보이는데, 그건 **질문 한 줄**만 처리하기 때문이다
(문서 임베딩은 적재할 때 이미 해 뒀다). 리랭킹과 색인은 **문서 본문 전체**를 통과시킨다.

CPU 로 운영해야 한다면 이것들을 조정해야 한다 — 자세한 건 `docs/질문_이해.md`:

```ini
RERANK_TOP_N=8 · RERANK_MAX_CHARS=600   # 리랭킹 8초 → 1초대
RERANK_ENABLED=false                     # 그래도 느리면 아예 끔
INGEST_WORKERS=1                         # 기본 4 는 GPU 기준. CPU 면 서버가 마비된다
```

#### GPU 이미지를 못 구하면 — vLLM 으로 임베딩을 서빙하는 길

**이미 Blackwell 에서 도는 vLLM 이 있다면** 그게 가장 확실한 길이다. vLLM 은 임베딩
모델도 서빙할 수 있어(`--task embed`) TEI 이미지 문제를 통째로 피한다. 남은 VRAM
(~26GB)에 568M 모델 하나는 충분히 들어간다.

```bash
vllm serve nlpai-lab/KURE-v1 --task embed --port 8081 \
  --gpu-memory-utilization 0.05
```

다만 API 모양이 다르다 — TEI 는 `POST /embed {"inputs": [...]}`, vLLM 은 OpenAI 형식인
`POST /v1/embeddings {"input": [...]}` 이다. 이 경로를 쓰려면 클라이언트를 하나 더
붙여야 한다(`app/clients/embedding.py` 옆에 OpenAI 형식 구현 추가). 필요하면 요청할 것.

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
