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
}


class DuplicateError(ValueError):
    def __init__(self, existing_doc_id: str) -> None:
        super().__init__(f"이미 적재된 문서(중복): {existing_doc_id}")
        self.existing_doc_id = existing_doc_id


class HashLookup(Protocol):
    """file_hash로 기존 doc_id를 반환(없으면 None). 구현은 Postgres 조회."""

    def __call__(self, file_hash: str) -> Optional[str]: ...


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
) -> IdentificationBlock:
    """파일을 수신하여 식별 블록을 만든다. 중복이면 DuplicateError."""
    filename = Path(path).name
    file_format = detect_format(filename)
    file_hash = compute_file_hash(path)

    if hash_lookup is not None:
        existing = hash_lookup(file_hash)
        if existing is not None:
            raise DuplicateError(existing)

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
        ingested_at=datetime.now(timezone.utc),
        ingested_by=ingested_by,
        page_count=page_count,
        last_modified=last_modified,
    )
