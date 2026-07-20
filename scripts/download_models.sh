#!/usr/bin/env bash
# 폐쇄망 배포용 모델 사전 다운로드.
# 인터넷 되는 머신에서 실행 → 생성된 ./models 폴더를 서버로 옮겨 컨테이너에 마운트한다.
#
# 사용:
#   pip install -U "huggingface_hub[cli]"
#   bash scripts/download_models.sh [DEST_DIR]     # 기본 DEST=./models
#
# 결과:
#   ./models/KURE-v1                (임베딩)
#   ./models/bge-reranker-v2-m3     (리랭커)
set -euo pipefail

DEST="${1:-./models}"
mkdir -p "$DEST"

dl() {
  local repo="$1" out="$2"
  echo ">> 다운로드: $repo -> $DEST/$out"
  if command -v hf >/dev/null 2>&1; then
    hf download "$repo" --local-dir "$DEST/$out"
  elif command -v huggingface-cli >/dev/null 2>&1; then
    huggingface-cli download "$repo" --local-dir "$DEST/$out"
  else
    echo "huggingface_hub CLI가 없습니다. 먼저: pip install -U \"huggingface_hub[cli]\"" >&2
    exit 1
  fi
}

dl "nlpai-lab/KURE-v1"          "KURE-v1"
dl "BAAI/bge-reranker-v2-m3"    "bge-reranker-v2-m3"

echo
echo "완료. '$DEST' 폴더를 서버의 repo 루트로 옮긴 뒤(./models), .env 에 아래를 설정:"
echo "  EMBEDDING_MODEL=/models/KURE-v1"
echo "  RERANKER_MODEL=/models/bge-reranker-v2-m3"
echo "  HF_HUB_OFFLINE=1"
echo "그다음: docker compose -f docker-compose.cpu.yml up -d"
