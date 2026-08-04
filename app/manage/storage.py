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


def sync_with_disk(session: Session, node_id: Optional[int] = None
                   ) -> tuple[list[str], list[str]]:
    """디스크의 저장 폴더와 화면의 폴더 목록을 맞춘다. → (새로 등록된, 새로 만든)

    - 서버에서 직접 만든 디렉터리 → 화면에 보이도록 **폴더 노드로 등록**
    - 화면에만 있고 디스크에 없는 폴더 → 디스크에 **디렉터리 생성**

    관리자가 누르는 '동기화' 버튼용이다. 폴더를 계속 감시하는 상주 프로세스를 두지
    않으려고 수동 실행 방식으로 만들었다. 부서(team/group/part)는 만들지 않는다 —
    조직도는 관리자만 바꿔야 하므로 디스크에 있는 미등록 디렉터리는 항상 폴더로 본다.
    """
    from app.db.repositories import OrgRepository
    from app.org.tree import FOLDER

    org = OrgRepository(session)
    registered: list[str] = []
    created: list[str] = []

    tree = org.load_tree()
    targets = tree.subtree(node_id) if node_id is not None else tree.node_ids()

    # 1) 화면 → 디스크: 아직 없는 폴더를 만든다
    for nid in targets:
        d = fs_dir(tree, nid)
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(" / ".join(tree.name_path(nid)))

    # 2) 디스크 → 화면: 등록 안 된 디렉터리를 폴더 노드로 등록(하위까지 재귀)
    def walk(parent_id: int) -> None:
        cur = org.load_tree()
        base = fs_dir(cur, parent_id)
        if not base.is_dir():
            return
        known = {}
        for child_id in cur.subtree(parent_id):
            child = org.get(child_id)
            if child is not None and child.parent_id == parent_id:
                known[slug(child.name)] = child_id
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith(".") or entry.name == UNFILED:
                continue
            child_id = known.get(entry.name)
            if child_id is None:
                node = org.create_node(entry.name, FOLDER, parent_id=parent_id)
                session.flush()
                child_id = node.id
                registered.append(" / ".join(org.load_tree().name_path(child_id)))
            walk(child_id)

    if node_id is not None:
        walk(node_id)                      # 지정 노드 아래만
    else:
        for nid in targets:                # 루트부터(하위는 walk 가 재귀로 처리)
            node = org.get(nid)
            if node is not None and node.parent_id is None:
                walk(nid)
    return registered, created


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
