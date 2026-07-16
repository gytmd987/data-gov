"""Controlled vocabularies (enums) for document metadata.

설계 원칙: LLM 자동 채움은 **이 고정 enum 안에서만** 값을 채울 수 있다(자유 텍스트 필드 금지).
enum 밖 후보나 낮은 신뢰도는 값을 비워두고(UNKNOWN) 사람이 검토 단계에서 확정한다.

허용값(vocabulary)은 Phase 0에서 인사팀과 함께 최종 확정한다. 아래는 초안이다.
"""

from __future__ import annotations

from enum import Enum


class FileFormat(str, Enum):
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    PDF = "pdf"
    JPG = "jpg"
    PNG = "png"
    TXT = "txt"


class Language(str, Enum):
    KO = "ko"
    EN = "en"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class DocType(str, Enum):
    """인사 문서 유형(초안). Phase 0에서 확정."""

    POLICY = "policy"                 # 인사 규정/정책
    CONTRACT = "contract"             # 근로계약/각종 계약
    PAYROLL = "payroll"               # 급여/보상 자료
    EVALUATION = "evaluation"         # 인사평가/성과
    RECRUITING = "recruiting"         # 채용/지원자
    TRAINING = "training"             # 교육/연수
    ATTENDANCE = "attendance"         # 근태/휴가
    ORG_CHART = "org_chart"           # 조직도/인원 현황
    MEETING_NOTE = "meeting_note"     # 회의록
    REPORT = "report"                 # 보고서/통계
    FORM_TEMPLATE = "form_template"   # 양식/서식
    OTHER = "other"
    UNKNOWN = "unknown"               # LLM이 확신 못 함 → 사람 확인 필요


class SensitivityLevel(str, Enum):
    """민감도 등급. 숫자가 클수록 민감. 접근통제 하드 필터의 clearance 비교에 사용."""

    PUBLIC = "public"                 # 사내 전체 공개
    INTERNAL = "internal"             # 인사팀 등 특정 그룹
    CONFIDENTIAL = "confidential"     # 제한된 담당자
    RESTRICTED = "restricted"         # 급여/평가/개인정보 등 최고 민감

    @property
    def rank(self) -> int:
        return {
            "public": 0,
            "internal": 1,
            "confidential": 2,
            "restricted": 3,
        }[self.value]


class PiiType(str, Enum):
    """개인정보 유형(초안)."""

    NAME = "name"
    RESIDENT_ID = "resident_id"       # 주민등록번호
    CONTACT = "contact"               # 연락처/이메일/주소
    SALARY = "salary"                 # 급여/보상
    EVALUATION = "evaluation"         # 평가 결과
    HEALTH = "health"                 # 건강/의료
    FAMILY = "family"                 # 가족관계
    ACCOUNT = "account"               # 계좌/금융
    OTHER = "other"


class DocStatus(str, Enum):
    """문서 생애주기 상태. 기본 검색은 active 만 노출."""

    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"         # 다른 문서로 대체됨
    EXPIRED = "expired"
    ARCHIVED = "archived"


class ChunkType(str, Enum):
    TEXT = "text"
    TABLE = "table"
    LIST = "list"
    IMAGE_CAPTION = "image_caption"
