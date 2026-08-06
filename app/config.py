"""애플리케이션 설정. .env 에서 로드한다(.env.example 참고)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 생성 LLM (이미 vLLM으로 기동 중)
    vllm_base_url: str = "http://localhost:8000/v1"
    vllm_api_key: str = "EMPTY"
    vllm_model: str = "thinkingcap"   # vLLM이 서빙하는 모델 id (/v1/models 와 일치)
    # 구조화 출력(적재 자동채움) 방식 — vLLM 버전에 맞춰 선택:
    #   guided_json     (기본) vLLM 확장 guided_json
    #   response_format OpenAI 표준 json_schema (최신 vLLM 권장)
    #   json_object     response_format json_object + 프롬프트에 스키마 안내(강제는 약함)
    vllm_structured_mode: str = "guided_json"
    # guided_json 백엔드(빈 값이면 미전송 → vLLM 기본값 사용). 예: xgrammar, outlines, lm-format-enforcer
    vllm_guided_backend: str = ""

    # 임베딩 / 리랭커 (TEI)
    embedding_model: str = "nlpai-lab/KURE-v1"   # 한국어 특화(BGE-M3 기반). 대안: BAAI/bge-m3
    embedding_port: int = 8081
    embedding_dim: int = 1024                     # KURE-v1(BGE-M3 계열) dense 차원. 모델 바꾸면 조정
    reranker_model: str = "BAAI/bge-reranker-v2-m3"  # 대안: dragonkue/bge-reranker-v2-m3-ko
    reranker_port: int = 8082
    tei_max_batch: int = 32   # TEI 기본 최대 클라이언트 배치. 초과하면 나눠서 요청

    # 파싱 / OCR
    # 스캔·이미지 OCR 백엔드: "vllm"(기본, 이미 뜬 Qwen3.6-27B 멀티모달 재사용, 추가 배포 0)
    #                       | "none"(OCR 비활성; 텍스트 경로만)
    #                       | "paddleocr-vl"(전용 파서, 정확도·처리량 필요 시 — 별도 배포)
    ocr_backend: str = "vllm"
    parser_model: str = "PaddleOCR-VL"   # ocr_backend="paddleocr-vl" 일 때 사용
    parser_device: str = "cuda"
    parser_lang: str = "korean"

    # 원본 파일 저장 위치(열람/다운로드용). 폐쇄망이면 로컬 경로로 충분.
    storage_dir: str = "./storage/originals"
    # 예약 업로드 대기 파일 보관 위치(웹에서 올려두고 야간에 처리할 파일)
    upload_queue_dir: str = "./storage/_queue"
    # 예약 업로드 처리 시간대(HH:MM). 시작>종료면 자정을 넘긴 것으로 본다(야간 처리).
    # 업무 시간에 GPU 를 점유해 채팅이 느려지지 않게 기본을 저녁~아침으로 둔다.
    ingest_window_start: str = "18:00"
    ingest_window_end: str = "08:00"
    ingest_workers: int = 4          # 예약 업로드 동시 처리 수
    # 표 데이터(명단·급여) 구조화 저장소(Tier 2 DuckDB 파일)
    duckdb_path: str = "./storage/datasets.duckdb"

    # 데이터 스토어
    postgres_user: str = "ragadmin"
    postgres_password: str = "changeme"
    postgres_db: str = "ragdb"
    postgres_port: int = 5432
    qdrant_http_port: int = 6333
    qdrant_grpc_port: int = 6334
    minio_root_user: str = "minioadmin"
    minio_root_password: str = "changeme"
    minio_port: int = 9000

    # 검색 파라미터
    # 어휘 검색(BM25) 축 사용 여부. 끄면 의미 검색(dense)만 쓴다.
    # 켠 뒤에는 기존 문서에 `python -m scripts.reindex_sparse` 를 한 번 돌려야
    # 옛 문서도 어휘 검색에 잡힌다(새로 등록하는 문서는 자동).
    hybrid_bm25: bool = True
    hybrid_top_n: int = 40
    rerank_top_k: int = 6

    @property
    def embedding_url(self) -> str:
        return f"http://localhost:{self.embedding_port}"

    @property
    def reranker_url(self) -> str:
        return f"http://localhost:{self.reranker_port}"

    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.postgres_user}:{self.postgres_password}"
            f"@localhost:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
