"""과거 문서 일괄 반입(one-off backfill).

폴더 구조가 곧 조직도 구조이고, 그게 그대로 **작성부서 · 열람 권한 · 저장 경로**가 된다.
조직도 모양대로 파일을 부어놓고 이 스크립트를 돌리면 권한을 한 건씩 지정할 필요가 없다.

    반입할문서/
      People팀/
        ㅁ그룹/
          ㄴ파트/  ← 이 안의 파일은 ㄴ파트 폴더, ㄴ파트 권한으로 등록

사용:
    # 1) 매핑 확인 (아무것도 적재하지 않음) — 반드시 먼저
    python -m scripts.bulk_ingest --dir 반입할문서 --dry-run

    # 2) 속도 측정 (20건만)
    python -m scripts.bulk_ingest --dir 반입할문서 --limit 20 --auto-confirm

    # 3) 본 실행 (야간). 중단해도 다시 돌리면 남은 것부터 이어감
    python -m scripts.bulk_ingest --dir 반입할문서 --auto-confirm --workers 8

이어하기는 파일 해시 중복 탐지로 자동 처리된다(이미 등록된 파일은 건너뜀).
"""

from __future__ import annotations

import argparse
import csv
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.ingestion.intake import SUPPORTED_SUFFIXES, DuplicateError
from app.ingestion.enrichment import ReadError

SUPPORTED_EXTS = SUPPORTED_SUFFIXES
UNFILED_LABEL = "(미분류 폴더)"

# 사람이 만든 파일이 아닌 것들 — 조용히 제외한다.
_SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}


def is_candidate(path: Path) -> bool:
    """반입 대상 파일인지. 숨김·오피스 임시파일·비지원 확장자는 제외."""
    name = path.name
    if name.startswith(".") or name.startswith("~$"):
        return False
    if name.lower() in _SKIP_NAMES:
        return False
    return path.suffix.lower() in SUPPORTED_EXTS


# ── 폴더 → 조직 노드 매핑 ────────────────────────────────────────────────────
@dataclass
class PlanItem:
    path: Path
    rel_dir: tuple[str, ...]          # base_dir 기준 상위 폴더 이름들
    node_id: Optional[int]            # 매칭된 조직 노드(없으면 None)
    node_label: str                   # 표시용 경로


def build_node_index(tree) -> dict[tuple[str, ...], int]:
    """{(루트…노드 이름): node_id}. 폴더 경로를 그대로 찾기 위한 색인."""
    index: dict[tuple[str, ...], int] = {}
    for node_id in tree.node_ids():
        index[tuple(tree.name_path(node_id))] = node_id
    return index


def resolve_node(index: dict[tuple[str, ...], int], rel_dir: tuple[str, ...],
                 root_path: tuple[str, ...]) -> Optional[int]:
    """폴더 경로 → 조직 노드 id.

    --root-node 를 주면 그 노드 아래에서 시작하는 것으로 본다(반입 폴더가 조직도
    중간부터 시작하는 경우). 전체 경로가 안 맞으면 뒤에서부터 줄여가며 가장 깊은
    상위 폴더를 찾는다 — 조직도에 없는 하위 폴더(예: '2024년')를 허용하기 위함.
    """
    parts = root_path + rel_dir
    while parts:
        if parts in index:
            return index[parts]
        parts = parts[:-1]
    return index.get(root_path) if root_path else None


def make_dirs(base_dir: Path, tree, root_node_id: Optional[int] = None
              ) -> tuple[list[str], list[str]]:
    """조직도 모양대로 빈 폴더 뼈대를 만든다. (만든 경로 목록, 건너뛴 사유 목록).

    폴더 이름이 조직도 이름과 한 글자라도 다르면 매칭이 안 되므로, 손으로 만들지 말고
    이걸로 만든 뒤 파일만 넣는 것을 권장한다. 이미 있는 폴더는 그대로 둔다(멱등).
    """
    root_path = tuple(tree.name_path(root_node_id)) if root_node_id else ()
    targets = tree.subtree(root_node_id) if root_node_id else tree.node_ids()
    created: list[str] = []
    skipped: list[str] = []
    for node_id in targets:
        names = tree.name_path(node_id)[len(root_path):]
        if not names:
            continue
        if any("/" in n or "\\" in n for n in names):
            skipped.append(f"{' / '.join(names)} (이름에 / 가 있어 폴더로 만들 수 없음)")
            continue
        path = base_dir.joinpath(*names)
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path.relative_to(base_dir)))
    return sorted(created), skipped


def make_plan(base_dir: Path, index: dict[tuple[str, ...], int],
              root_path: tuple[str, ...], tree) -> list[PlanItem]:
    items: list[PlanItem] = []
    for path in sorted(base_dir.rglob("*")):
        if not path.is_file() or not is_candidate(path):
            continue
        rel_dir = path.relative_to(base_dir).parts[:-1]
        node_id = resolve_node(index, rel_dir, root_path)
        label = " / ".join(tree.name_path(node_id)) if node_id else UNFILED_LABEL
        items.append(PlanItem(path=path, rel_dir=rel_dir, node_id=node_id,
                              node_label=label))
    return items


# ── 진행 상황 ────────────────────────────────────────────────────────────────
@dataclass
class Progress:
    total: int
    started: float = field(default_factory=time.monotonic)
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _last_print: float = 0.0

    @property
    def done(self) -> int:
        return self.ok + self.skipped + self.failed

    def record(self, kind: str, note: str = "") -> None:
        with self._lock:
            setattr(self, kind, getattr(self, kind) + 1)
            now = time.monotonic()
            if now - self._last_print >= 5 or self.done == self.total:
                self._last_print = now
                self._print(note)

    def _print(self, note: str) -> None:
        elapsed = max(0.001, time.monotonic() - self.started)
        rate = self.done / elapsed                      # 건/초
        remain = self.total - self.done
        eta = remain / rate if rate > 0 else 0
        print(f"  [{self.done}/{self.total}] 등록 {self.ok} · 건너뜀 {self.skipped}"
              f" · 실패 {self.failed} · {rate * 3600:.0f}건/시간"
              f" · 남은 시간 {_hms(eta)}{'  ' + note if note else ''}", flush=True)


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}시간 {s % 3600 // 60}분" if s >= 3600 else f"{s // 60}분 {s % 60}초"


# ── 파일 1건 처리 ────────────────────────────────────────────────────────────
_local = threading.local()


def _service():
    """스레드마다 자기 세션·서비스를 쓴다(SQLAlchemy 세션은 스레드 공유 불가)."""
    svc = getattr(_local, "svc", None)
    if svc is None:
        from app.review.factory import build_service
        svc = _local.svc = build_service()
    return svc


def ingest_one(item: PlanItem, ingested_by: str, auto_confirm: bool) -> tuple[str, str]:
    """(결과종류, 메모). 결과종류 ∈ {ok, skipped, failed}."""
    svc = _service()
    try:
        doc_id = svc.start_ingestion(str(item.path), ingested_by=ingested_by,
                                     folder_node_id=item.node_id)
    except DuplicateError:
        return "skipped", "이미 등록됨"
    except ReadError as e:
        svc.session.rollback()
        return "failed", str(e)
    except Exception as e:  # noqa: BLE001 — 한 건 실패가 전체를 멈추면 안 된다
        svc.session.rollback()
        return "failed", f"{type(e).__name__}: {e}"

    if not auto_confirm:
        return "ok", "검토 대기"

    try:
        confirm(svc, doc_id)
    except Exception as e:  # noqa: BLE001
        svc.session.rollback()
        return "failed", f"등록 확정 실패: {type(e).__name__}: {e}"
    return "ok", "등록 완료"


def confirm(svc, doc_id: str) -> None:
    """검토 없이 등록 확정. 예약 업로드 워커와 같은 로직을 쓴다."""
    svc.confirm_without_review(doc_id)


# ── dry-run 리포트 ───────────────────────────────────────────────────────────
def print_plan(items: list[PlanItem], auto_confirm: bool) -> None:
    by_node: dict[str, list[PlanItem]] = {}
    for it in items:
        by_node.setdefault(it.node_label, []).append(it)

    print(f"\n반입 대상 {len(items)}건 — 폴더(작성부서·열람권한)별 분류\n")
    for label in sorted(by_node, key=lambda x: (x == UNFILED_LABEL, x)):
        group = by_node[label]
        mark = "  ⚠️ " if label == UNFILED_LABEL else "  "
        print(f"{mark}{label}: {len(group)}건")
        for it in group[:3]:
            print(f"        · {it.path.name}")
        if len(group) > 3:
            print(f"        · … 외 {len(group) - 3}건")

    _print_title_preview(items)

    unfiled = by_node.get(UNFILED_LABEL, [])
    if unfiled:
        print(f"\n⚠️  조직도에 없는 폴더에 있는 파일이 {len(unfiled)}건입니다.")
        print("    이대로 실행하면 작성부서 없이(= 팀 전체 공개) 등록됩니다.")
        print("    폴더 이름을 조직도와 맞추거나 --root-node 로 시작 노드를 지정하세요.")
    print(f"\n등록 방식: {'자동 확정(즉시 검색 노출)' if auto_confirm else '검토 대기(사람이 확정해야 노출)'}")
    print("실제로 적재하려면 --dry-run 을 빼고 다시 실행하세요.\n")


# ── main ─────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="과거 문서 일괄 반입")
    ap.add_argument("--dir", required=True, help="반입할 문서가 든 폴더(하위 폴더 포함)")
    ap.add_argument("--root-node", default=None,
                    help="반입 폴더가 대응되는 조직 노드(이름 또는 id). "
                         "생략하면 폴더 이름이 조직도 최상위부터 일치해야 함")
    ap.add_argument("--make-dirs", action="store_true",
                    help="조직도 모양대로 빈 폴더만 만들고 종료(파일 넣기 전 준비 단계)")
    ap.add_argument("--dry-run", action="store_true", help="적재 없이 매핑만 확인")
    ap.add_argument("--limit", type=int, default=0, help="앞에서 N건만 처리(속도 측정용)")
    ap.add_argument("--auto-confirm", action="store_true",
                    help="검토 없이 즉시 등록(대량 반입용). 생략하면 검토 대기로 쌓임")
    ap.add_argument("--workers", type=int, default=8,
                    help="동시 처리 수(기본 8). vLLM 이 배칭하므로 8~16 권장")
    ap.add_argument("--ingested-by", default="bulk-ingest",
                    help="등록자로 기록할 아이디")
    ap.add_argument("--report", default="bulk_ingest_failed.csv",
                    help="실패 목록 CSV 경로")
    args = ap.parse_args(argv)

    base_dir = Path(args.dir).expanduser().resolve()
    if args.make_dirs:
        try:                       # 준비 단계라 반입 폴더가 없으면 만들어 준다
            base_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"⛔ 폴더를 만들 수 없습니다: {base_dir}\n   ({e})")
            print("   쓰기 권한이 있는 경로를 쓰거나, 먼저 만들어 주세요:")
            print(f"     sudo mkdir -p {base_dir} && sudo chown $USER {base_dir}")
            return 2
    if not base_dir.is_dir():
        print(f"⛔ 폴더가 없습니다: {base_dir}")
        print("   경로를 확인하세요. 반입 폴더를 새로 만들려면 --make-dirs 를 붙이세요.")
        return 2

    from app.db.repositories import OrgRepository
    from app.review.factory import new_session
    session = new_session()
    try:
        org = OrgRepository(session)
        tree = org.load_tree()
        index = build_node_index(tree)
        root_node_id: Optional[int] = None
        root_path: tuple[str, ...] = ()
        if args.root_node:
            root_node_id = _find_node(org, tree, args.root_node)
            if root_node_id is None:
                print(f"⛔ 조직도에서 '{args.root_node}' 를 찾지 못했습니다.")
                return 2
            root_path = tuple(tree.name_path(root_node_id))
            print(f"시작 노드: {' / '.join(root_path)}")

        if args.make_dirs:
            created, skipped = make_dirs(base_dir, tree, root_node_id)
            print(f"\n조직도 모양으로 폴더를 만들었습니다: {base_dir}\n")
            for rel in created:
                print(f"  + {rel}")
            if not created:
                print("  (이미 모두 있습니다)")
            for why in skipped:
                print(f"  ⚠️ 건너뜀: {why}")
            print("\n이제 각 폴더에 문서를 넣은 뒤 --dry-run 으로 확인하세요.")
            return 0

        items = make_plan(base_dir, index, root_path, tree)
    finally:
        session.close()

    if not items:
        print(f"반입할 파일이 없습니다. (지원 형식: {', '.join(sorted(SUPPORTED_EXTS))})")
        return 0
    if args.limit:
        items = items[:args.limit]

    if args.dry_run:
        print_plan(items, args.auto_confirm)
        return 0

    print(f"\n반입 시작 — {len(items)}건 · 동시 {args.workers} · "
          f"{'자동 확정' if args.auto_confirm else '검토 대기'}")
    print("중단해도(Ctrl+C) 다시 실행하면 남은 것부터 이어집니다.\n")

    progress = Progress(total=len(items))
    failures: list[tuple[str, str]] = []
    fail_lock = threading.Lock()

    def work(item: PlanItem):
        kind, note = ingest_one(item, args.ingested_by, args.auto_confirm)
        if kind == "failed":
            with fail_lock:
                failures.append((str(item.path), note))
        progress.record(kind)
        return kind

    try:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [pool.submit(work, it) for it in items]
            for _ in as_completed(futures):
                pass
    except KeyboardInterrupt:
        print("\n중단했습니다. 지금까지 처리한 내용은 저장돼 있습니다.")

    elapsed = time.monotonic() - progress.started
    print(f"\n완료 — 등록 {progress.ok} · 건너뜀(이미 등록) {progress.skipped} · "
          f"실패 {progress.failed} · 소요 {_hms(elapsed)}")
    if progress.ok:
        print(f"처리 속도: {progress.done / max(elapsed, 0.001) * 3600:.0f}건/시간")
    if failures:
        _write_report(Path(args.report), failures)
        print(f"실패 목록: {args.report} ({len(failures)}건) — 원인을 고친 뒤 "
              "같은 명령을 다시 실행하면 실패분만 재시도됩니다.")
    return 0


def _find_node(org, tree, key: str) -> Optional[int]:
    """--root-node 값(id 또는 이름)으로 조직 노드 찾기."""
    try:
        node_id = int(key)
        return node_id if tree.get(node_id) is not None else None
    except ValueError:
        pass
    matches = [n.id for n in org.list_nodes() if n.name == key]
    return matches[0] if len(matches) == 1 else None


def _write_report(path: Path, failures: list[tuple[str, str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["파일", "실패 사유"])
        w.writerows(failures)


if __name__ == "__main__":
    sys.exit(main())
