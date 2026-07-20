# 폐쇄망 모델 사전 다운로드 (HuggingFace 접속 차단 환경)

사내망에서 TEI 컨테이너가 HuggingFace에 접속할 수 없으면, 모델을 **인터넷 되는 곳에서 미리 받아
서버로 옮겨 로컬 경로로 마운트**한다. HF 접속 없이 로드된다.

필요한 모델 2개:
- 임베딩: `nlpai-lab/KURE-v1`
- 리랭커: `BAAI/bge-reranker-v2-m3`

(스캔·이미지 OCR은 이미 뜬 vLLM(Qwen3.6-27B)을 재사용하므로 추가 모델 불필요. 생성 LLM도 vLLM에서
별도로 준비된 것을 사용한다.)

## 1. 인터넷 되는 머신에서 다운로드

```bash
pip install -U "huggingface_hub[cli]"
bash scripts/download_models.sh        # ./models/KURE-v1, ./models/bge-reranker-v2-m3 생성
```

수동으로 받으려면:

```bash
hf download nlpai-lab/KURE-v1       --local-dir ./models/KURE-v1
hf download BAAI/bge-reranker-v2-m3 --local-dir ./models/bge-reranker-v2-m3
```

각 폴더에 `config.json`, 토크나이저, 모델 가중치(`*.safetensors`)가 들어 있어야 한다.

## 2. 서버로 전송

`models/` 폴더 전체를 서버의 repo 루트로 옮긴다(scp/USB 등):

```bash
scp -r ./models  user@server:/path/to/data-gov/models
```

배치 결과(서버):
```
data-gov/
  docker-compose.cpu.yml
  models/
    KURE-v1/                 # config.json, tokenizer, *.safetensors ...
    bge-reranker-v2-m3/
```

## 3. .env 에 로컬 경로 지정

```bash
# .env
EMBEDDING_MODEL=/models/KURE-v1
RERANKER_MODEL=/models/bge-reranker-v2-m3
HF_HUB_OFFLINE=1
```

compose가 `./models`를 컨테이너의 `/models`(읽기전용)로 마운트하고, TEI는 이 로컬 경로에서 모델을 로드한다.

## 4. 기동 + 확인

```bash
docker compose -f docker-compose.cpu.yml up -d
docker compose -f docker-compose.cpu.yml logs embedding --tail=20   # "Ready" (다운로드 시도 없이)
python -m scripts.preflight
```

## 참고

- **도커 이미지도 막혀 있으면**(ghcr.io/docker.io 접속 불가): 인터넷 머신에서
  `docker pull <img> && docker save <img> -o img.tar` → 서버로 옮겨 `docker load -i img.tar`.
  필요한 이미지: `ghcr.io/huggingface/text-embeddings-inference:cpu-latest`, `postgres:16`,
  `qdrant/qdrant:latest`, `minio/minio:latest`.
- **GPU TEI**를 쓰려면 같은 방식으로 `docker-compose.yml`(GPU)에서 마운트·경로를 지정하면 된다
  (단, Blackwell 이미지 호환은 별도 확인 — `docs/smoke_test.md`).
- 모델 폴더는 용량이 크므로 git에 커밋하지 않는다(`.gitignore`의 `models/`).
