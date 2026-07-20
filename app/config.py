"""애플리케이션 설정. .env 에서 로드한다(.env.example 참고)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 생성 LLM (이미 vLLM으로 기동 중)
    vllm_base_url: str = "http://localhost:8000/v1"
    vllm_api_key: str = "EMPTY"
    vllm_model: str = "Qwen/Qwen3.6-27B"

    # 임베딩 / 리랭커 (TEI)
    embedding_model: str = "nlpai-lab/KURE-v1"   # 한국어 특화(BGE-M3 기반). 대안: BAAI/bge-m3
    embedding_port: int = 8081
    embedding_dim: int = 1024                     # KURE-v1(BGE-M3 계열) dense 차원. 모델 바꾸면 조정
    reranker_model: str = "BAAI/bge-reranker-v2-m3"  # 대안: dragonkue/bge-reranker-v2-m3-ko
    reranker_port: int = 8082

    # 파싱 / OCR
    # 스캔·이미지 OCR 백엔드: "vllm"(기본, 이미 뜬 Qwen3.6-27B 멀티모달 재사용, 추가 배포 0)
    #                       | "none"(OCR 비활성; 텍스트 경로만)
    #                       | "paddleocr-vl"(전용 파서, 정확도·처리량 필요 시 — 별도 배포)
    ocr_backend: str = "vllm"
    parser_model: str = "PaddleOCR-VL"   # ocr_backend="paddleocr-vl" 일 때 사용
    parser_device: str = "cuda"
    parser_lang: str = "korean"

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
