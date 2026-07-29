"""원본 파일 저장 위치 = 조직도 폴더 미러링.

문서의 '폴더'는 곧 그 문서의 조직 노드(author_node_id, 작성/관리 부서)다.
디스크에도 같은 모양으로 저장한다:

    storage/originals/People팀/채용그룹/인터뷰파트/(25-0728) 면접 가이드.docx

- 폴더(노드)를 바꾸면 파일을 이동한다(place).
- 노드 이름이 바뀌면 subtree 문서를 새 경로로 재배치한다(relocate_subtree, 멱등).
- 노드가 지정되지 않은 문서는 `_미분류` 폴더에 둔다.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.ingestion.titletools import safe_filename

UNFILED = "_미분류"

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def slug(name: str) -> str:
    """폴더 이름으로 안전한 문자열(한글 유지, 경로 구분자·특수문자만 제거)."""
    s = _BAD.sub("_", (name or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:80] or UNFILED


def fs_dir(tree, node_id: Optional[int]) -> Path:
    """이 노드(폴더)의 디스크 경로. 노드가 없으면 `_미분류`."""
    root = Path(settings.storage_dir)
    if node_id is None:
        return root / UNFILED
    names = tree.name_path(node_id)
    if not names:
        return root / UNFILED
    path = root
    for n in names:
        path = path / slug(n)
    return path


def target_path(tree, node_id: Optional[int], title: str, ext: str,
                doc_id: str = "") -> Path:
    """폴더 경로 + 제목 기반 파일명. ext 는 '.docx' 형태 또는 확장자 문자열."""
    ext = ext if ext.startswith(".") else f".{ext}"
    return fs_dir(tree, node_id) / f"{safe_filename(title)}{ext}"


def _unique(dest: Path, doc_id: str) -> Path:
    """같은 이름이 이미 있으면 doc_id 앞 6자를 붙여 충돌 회피."""
    if not dest.exists():
        return dest
    return dest.with_name(f"{dest.stem}_{doc_id[:6]}{dest.suffix}")


def place(session: Session, doc_id: str, node_id: Optional[int],
          title: str, ext: str) -> Optional[str]:
    """문서 원본을 해당 폴더(노드) 경로로 이동/배치하고 original_path 를 갱신한다.

    이미 목표 경로면 아무것도 하지 않는다(멱등). 반환: 최종 경로(없으면 None).
    """
    from app.db.repositories import DocumentRepository, OrgRepository

    repo = DocumentRepository(session)
    src = repo.get_original_path(doc_id)
    if not src or not os.path.exists(src):
        return None

    tree = OrgRepository(session).load_tree()
    suffix = Path(src).suffix or (ext if ext.startswith(".") else f".{ext}")
    dest = target_path(tree, node_id, title, suffix, doc_id)
    if Path(src).resolve() == dest.resolve():
        return src
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest = _unique(dest, doc_id)
    try:
        os.replace(src, dest)
    except OSError:
        return src   # 이동 실패해도 기존 경로 유지(다운로드는 계속 동작)
    repo.set_original_path(doc_id, str(dest))
    return str(dest)


def relocate_subtree(session: Session, node_id: int) -> int:
    """노드 이름 변경 등으로 경로가 어긋난 subtree 문서를 제자리로 재배치.

    반환: 이동한 문서 수. 멱등(이미 맞으면 건너뜀).
    """
    from app.db.repositories import DocumentRepository, OrgRepository

    org = OrgRepository(session)
    tree = org.load_tree()
    ids = set(tree.subtree(node_id))
    if not ids:
        return 0
    repo = DocumentRepository(session)
    moved = 0
    for row in repo.list_documents(author_node_ids=ids):
        before = repo.get_original_path(row["doc_id"])
        if not before:
            continue
        after = place(session, row["doc_id"], row["author_node_id"],
                      row["title"] or row["filename"], Path(before).suffix)
        if after and after != before:
            moved += 1
    return moved
