"""파일 수신 단계: 해시·중복 탐지·시스템 자동 식별 필드 생성.

여기서 IdentificationBlock(시스템 자동 필드)을 채우고, file_hash로 기존 문서와의
중복을 판단한다. 실제 중복 저장소는 Postgres이지만, 여기서는 조회 콜백(Protocol)만 의존해
단위 테스트가 외부 DB 없이 가능하도록 한다.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

from app.schemas.enums import FileFormat
from app.schemas.metadata import IdentificationBlock

_EXT_TO_FORMAT = {
    ".docx": FileFormat.DOCX,
    ".pptx": FileFormat.PPTX,
    ".xlsx": FileFormat.XLSX,
    ".pdf": FileFormat.PDF,
    ".jpg": FileFormat.JPG,
    ".jpeg": FileFormat.JPG,
    ".png": FileFormat.PNG,
    ".txt": FileFormat.TXT,
    ".eml": FileFormat.EMAIL,
    ".mysingle": FileFormat.EMAIL,   # 사내 그룹웨어 메일 — 내용은 표준 메일이다
}

# 포맷 → 실제 파일 확장자. 보관·다운로드 파일명에 쓴다.
# (enum 값을 그대로 쓰면 메일이 `제목.email` 이 되어 더블클릭으로 안 열린다)
EXT_BY_FORMAT = {
    FileFormat.DOCX: ".docx",
    FileFormat.PPTX: ".pptx",
    FileFormat.XLSX: ".xlsx",
    FileFormat.PDF: ".pdf",
    FileFormat.JPG: ".jpg",
    FileFormat.PNG: ".png",
    FileFormat.TXT: ".txt",
    FileFormat.EMAIL: ".eml",
}


SUPPORTED_SUFFIXES = frozenset(_EXT_TO_FORMAT)


def ext_for(file_format: FileFormat) -> str:
    return EXT_BY_FORMAT.get(file_format, f".{file_format.value}")


class DuplicateError(ValueError):
    def __init__(self, existing_doc_id: str, reason: str = "내용 동일") -> None:
        super().__init__(f"이미 적재된 문서({reason}): {existing_doc_id}")
        self.existing_doc_id = existing_doc_id
        self.reason = reason


class HashLookup(Protocol):
    """file_hash로 기존 doc_id를 반환(없으면 None). 구현은 Postgres 조회."""

    def __call__(self, file_hash: str) -> Optional[str]: ...


class MessageIdLookup(Protocol):
    """Message-ID로 기존 doc_id를 반환(없으면 None)."""

    def __call__(self, message_id: str) -> Optional[str]: ...


def detect_format(filename: str) -> FileFormat:
    ext = Path(filename).suffix.lower()
    fmt = _EXT_TO_FORMAT.get(ext)
    if fmt is None:
        raise ValueError(f"지원하지 않는 확장자: {ext}")
    return fmt


def compute_file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def intake(
    path: str,
    ingested_by: str,
    hash_lookup: Optional[HashLookup] = None,
    page_count: Optional[int] = None,
    message_id_lookup: Optional[MessageIdLookup] = None,
    source_filename: Optional[str] = None,
) -> IdentificationBlock:
    """파일을 수신하여 식별 블록을 만든다. 중복이면 DuplicateError.

    source_filename 을 주면 그 이름을 기록한다(형식 변환 전 원래 이름 보존용).
    """
    filename = source_filename or Path(path).name
    file_format = detect_format(filename)
    file_hash = compute_file_hash(path)

    if hash_lookup is not None:
        existing = hash_lookup(file_hash)
        if existing is not None:
            raise DuplicateError(existing)

    # 메일은 파일 해시로 부족하다. 같은 메일이라도 사서함마다 헤더(Received 등)가 달라
    # 해시가 어긋나기 때문이다. 발신 시점에 정해지는 Message-ID 로 한 번 더 본다.
    message_id = None
    if file_format is FileFormat.EMAIL:
        from app.ingestion.mailfile import message_id_of
        message_id = message_id_of(path)
        if message_id and message_id_lookup is not None:
            existing = message_id_lookup(message_id)
            if existing is not None:
                raise DuplicateError(existing, reason="같은 메일")

    # 최종 수정일: 파일시스템 mtime(오피스 내부 속성이 있으면 파서가 보정 가능)
    last_modified = None
    try:
        last_modified = datetime.fromtimestamp(Path(path).stat().st_mtime, tz=timezone.utc)
    except OSError:
        pass

    return IdentificationBlock(
        doc_id=str(uuid.uuid4()),
        source_filename=filename,
        file_format=file_format,
        file_hash=file_hash,
        message_id=message_id,
        ingested_at=datetime.now(timezone.utc),
        ingested_by=ingested_by,
        page_count=page_count,
        last_modified=last_modified,
    )
