"""SQLAlchemy ORM 모델 (영속 계층).

포터블하게 sqlalchemy.JSON/Date 를 사용해 Postgres(운영) / SQLite(테스트) 모두 지원한다.
벡터는 Qdrant에 있고, 여기서는 문서·청크·사용자·그룹·감사로그를 영속화한다.

denormalized 컬럼(status, file_hash, sensitivity_level 등)은 조회·중복탐지·필터용이고,
전체 메타데이터 원본은 documents.metadata_json 에 보존한다.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_filename: Mapped[str] = mapped_column(String(512))
    file_format: Mapped[str] = mapped_column(String(16))
    file_hash: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    original_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # 원본 파일 경로

    # 적재 상태 머신
    status: Mapped[str] = mapped_column(String(32), index=True)

    # 분류(조회용 denormalized)
    doc_type: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # 거버넌스(조회/필터용 denormalized)
    sensitivity_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    contains_pii: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    access_groups: Mapped[list] = mapped_column(JSON, default=list)

    # 작성자 소속 노드(부서장 관리 범위 판정용, denormalized)
    author_node_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    # 생애주기
    lifecycle_status: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    superseded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 전체 메타데이터 원본
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)

    # 유사(개정판 가능) 후보 — 적재 시 자동 탐지, 검토 화면에서 사람이 판단
    similar_candidates: Mapped[list] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, index=True)

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("file_hash", name="uq_documents_file_hash"),
    )


class UploadJob(Base):
    """예약 업로드 대기열 — 웹에서 올린 파일을 **나중에** 처리하기 위한 작업 한 건.

    업로드 요청 안에서 파싱·AI 자동 채움을 돌리면 문서당 수십 초라 대량 업로드가
    불가능하다(요청이 끊긴다). 그래서 업로드는 '파일 저장 + 이 행 생성'까지만 하고,
    실제 처리는 야간 워커(scripts/ingest_worker.py)가 맡는다.

    상태: queued → processing → done / failed
    """

    __tablename__ = "upload_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    path: Mapped[str] = mapped_column(String(1024))            # 대기 폴더에 저장된 파일
    source_filename: Mapped[str] = mapped_column(String(512))  # 사용자가 올린 원래 이름
    folder_node_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(128), index=True)
    batch: Mapped[str] = mapped_column(String(64), index=True)  # 한 번의 업로드 묶음

    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentAccessToken(Base):
    """문서 열람 토큰(access_groups)을 행으로 펼친 검색용 테이블.

    access_groups 는 JSON 배열이라 SQL 에서 '이 사용자가 읽을 수 있는 문서' 를
    포터블하게 필터할 수 없다. 그래서 upsert_document 시점에 같은 값을 여기에
    펼쳐 두고, 목록·검색 쿼리는 이 테이블을 조인해 **DB 단에서** 권한을 건다.
    (화면에서 거르는 방식은 페이징·건수와 어긋나고 유출 위험이 있다.)
    """

    __tablename__ = "document_access_tokens"

    doc_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("documents.doc_id", ondelete="CASCADE"), primary_key=True)
    token: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)


class Chunk(Base):
    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    parent_doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.doc_id", ondelete="CASCADE"), index=True)
    chunk_type: Mapped[str] = mapped_column(String(16))
    section_title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    indexed: Mapped[bool] = mapped_column(Boolean, default=False)

    document: Mapped[Document] = relationship(back_populates="chunks")


class Group(Base):
    __tablename__ = "groups"

    group_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)


class OrgNode(Base):
    """조직도 노드 — People팀(team) > 그룹(group) > 파트(part) 계층 + 하위 폴더(folder).

    부서(team/group/part)는 관리자만 만든다. 그 아래 **정리용 하위 폴더(folder)** 는
    해당 부서 구성원도 만들 수 있고, 저장 경로와 문서 분류에만 쓰인다.
    **폴더는 권한 주체가 아니다** — 열람 권한은 항상 부서 단위로 표현된다.
    """

    __tablename__ = "org_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    node_type: Mapped[str] = mapped_column(String(16))   # team | group | part | folder
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_nodes.id", ondelete="CASCADE"), nullable=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # 이 조직의 리더(부서장) — 조직도 관리에서 지정. 접근 토큰 h:{node} 의 근거.
    leader_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # 이 폴더에 문서를 올릴 때 열람 권한 기본값(access_selections 형식, 예: ["node:3"]).
    # **기본값일 뿐 강제가 아니다** — 검토 화면에서 사람이 바꿀 수 있다.
    default_access: Mapped[list] = mapped_column(JSON, default=list)


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    position: Mapped[str | None] = mapped_column(String(64), nullable=True)   # 직책(레거시)
    job: Mapped[str | None] = mapped_column(String(64), nullable=True)        # 직무(레거시)


class UserOrgNode(Base):
    """사용자 ↔ 조직 노드 소속(다대다). 한 사람이 여러 부서에 속할 수 있다."""

    __tablename__ = "user_org_nodes"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    node_id: Mapped[int] = mapped_column(
        ForeignKey("org_nodes.id", ondelete="CASCADE"), primary_key=True)


class UserGroup(Base):
    __tablename__ = "user_groups"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    group_name: Mapped[str] = mapped_column(
        ForeignKey("groups.group_name", ondelete="CASCADE"), primary_key=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(32), index=True)
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    event: Mapped[dict] = mapped_column(JSON, default=dict)   # 상세(필터/검색·인용 id 등)


class Feedback(Base):
    """답변 피드백 — 사용자가 답변이 맞았는지/틀렸는지 + 교정 메모."""

    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    query_text: Mapped[str] = mapped_column(Text)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    rating: Mapped[str] = mapped_column(String(8))            # "up" | "down"
    note: Mapped[str | None] = mapped_column(Text, nullable=True)   # 무엇이 틀렸는지/정답
    cited_doc_ids: Mapped[list] = mapped_column(JSON, default=list)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)  # 관리자 처리 완료 여부


class DocumentRequest(Base):
    """문서 수정/삭제 요청 — 파트원은 직접 수정·삭제 못 하고 요청만 한다.

    승인권: 관리자 전체, 부서장은 자기 부서(subtree) 문서. 삭제는 승인 시 하드 삭제.
    """

    __tablename__ = "document_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(String(64), index=True)
    doc_title: Mapped[str | None] = mapped_column(String(1024), nullable=True)  # 표시용 스냅샷
    author_node_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # 승인 범위 판정
    request_type: Mapped[str] = mapped_column(String(16))     # edit | delete
    requester_id: Mapped[str] = mapped_column(String(128), index=True)
    target_admin_id: Mapped[str | None] = mapped_column(String(128), nullable=True)  # 삭제: 지정 관리자
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)   # 수정 제안 내용
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|approved|rejected
    resolved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentRelation(Base):
    """문서 간 '연관' 관계(무방향). 별첨·참고·연관을 타입 구분 없이 하나로 기록한다.

    업로드 시 자동 감지(파일명·본문 언급·내용 유사)해 연결하고, 사람은 틀린 것만 제거한다.
    쌍은 (doc_a < doc_b) 로 정규화해 중복 없이 한 행만 둔다.
    """

    __tablename__ = "document_relations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_a: Mapped[str] = mapped_column(String(64), index=True)
    doc_b: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(16), default="auto")   # auto | human
    confidence: Mapped[float] = mapped_column(default=1.0)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 파일명 | 본문 언급 | 내용 유사
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    __table_args__ = (
        UniqueConstraint("doc_a", "doc_b", name="uq_document_relations_pair"),
    )


class Dataset(Base):
    """표 데이터(명단·급여) 구조화 카탈로그 — DuckDB 테이블 1개와 1:1.

    라우팅/권한의 근거. access_groups 는 문서 열람 토큰을 그대로 상속한다.
    """

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(
        ForeignKey("documents.doc_id", ondelete="CASCADE"), index=True)
    sheet: Mapped[str] = mapped_column(String(256))
    table_name: Mapped[str] = mapped_column(String(160), unique=True)   # DuckDB 물리 테이블
    columns_json: Mapped[list] = mapped_column(JSON, default=list)       # [{name, type}]
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    access_groups: Mapped[list] = mapped_column(JSON, default=list)      # 문서 열람 토큰 상속
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Conversation(Base):
    """채팅 대화(ChatGPT 스타일 대화 목록의 한 항목)."""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)  # 첫 질문으로 자동
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan",
        order_by="ChatMessage.id")


class ChatMessage(Base):
    """대화 내 메시지 1건 (user 또는 assistant)."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))       # "user" | "assistant"
    text: Mapped[str] = mapped_column(Text)
    use_rag: Mapped[bool] = mapped_column(Boolean, default=False)  # 팀 데이터 기반 여부
    sources_json: Mapped[list] = mapped_column(JSON, default=list)  # group_sources 결과
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
