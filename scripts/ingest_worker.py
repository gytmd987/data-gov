"""예약 업로드 처리 워커 — 웹에서 올려둔 파일을 **정해진 시간대에** 자동 등록한다.

웹 업로드는 요청 안에서 파싱·AI 자동 채움을 돌릴 수 없다(문서당 수십 초라 요청이
끊긴다). 그래서 화면에서는 파일만 받아 대기열에 넣고, 실제 처리는 이 워커가 맡는다.
검토는 생략하고 바로 등록한다 — 권한·작성부서는 AI 가 아니라 **업로드할 때 고른
폴더**에서 오므로 사람 확인 없이도 안전하다.

    python -m scripts.ingest_worker              # 상주 실행(시간대 밖이면 대기)
    python -m scripts.ingest_worker --now        # 시간대 무시하고 지금 처리
    python -m scripts.ingest_worker --once       # 대기열을 한 번만 비우고 종료

기본 처리 시간대는 18:00~08:00(설정 INGEST_WINDOW_START/END). 업무 시간에 GPU 를
점유해 채팅이 느려지지 않게 하기 위함이다.

상주 실행은 systemd 로 띄우는 것을 권장한다(docs/예약업로드.md).
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from app.config import settings
from app.db.repositories import UploadJobRepository
from app.ingestion.enrichment import ReadError
from app.ingestion.intake import DuplicateError
from app.manage.schedule import (
    beat,
    describe,
    discard_staged,
    in_window,
    now_local,
    seconds_until,
    tz_of,
    window_from_settings,
)

IDLE_SLEEP = 5.0          # 대기열이 비었을 때 쉬는 시간(초)
STALE_MINUTES = 30        # 이보다 오래 processing 이면 워커가 죽은 것으로 보고 회수

_stop = threading.Event()
_local = threading.local()


def _service():
    """스레드마다 자기 세션·서비스(SQLAlchemy 세션은 스레드 간 공유 불가)."""
    svc = getattr(_local, "svc", None)
    if svc is None:
        from app.review.factory import build_service
        svc = _local.svc = build_service()
    return svc


def _jobs(session=None) -> UploadJobRepository:
    """작업 큐 저장소 — 서비스 세션을 그대로 쓴다(같은 트랜잭션 경계)."""
    return UploadJobRepository(session or _service().session)


def _urgent_waiting() -> bool:
    """시간대 밖이어도 처리해야 할 '지금 바로' 작업이 있나."""
    return _jobs().claimable(window_open=False) > 0


def process_one(job) -> tuple[str, str]:
    """대기열 작업 1건 처리. → (결과, 메모). 결과 ∈ {done, skipped, failed}"""
    import os

    svc = _service()
    if not os.path.exists(job.path):
        return "failed", f"대기 파일이 없습니다: {job.path}"
    try:
        doc_id = svc.start_ingestion(job.path, ingested_by=job.uploaded_by,
                                     folder_node_id=job.folder_node_id)
    except DuplicateError as e:
        return "skipped", f"이미 등록된 문서({e.reason})"
    except ReadError as e:
        svc.session.rollback()
        return "failed", str(e)
    except Exception as e:                       # noqa: BLE001
        svc.session.rollback()
        return "failed", f"{type(e).__name__}: {e}"

    try:
        svc.confirm_without_review(doc_id)
    except Exception as e:                       # noqa: BLE001
        svc.session.rollback()
        return "failed", f"등록 확정 실패: {type(e).__name__}: {e}"
    return "done", doc_id


def _handle(job) -> str:
    """처리 + 결과 기록 + 대기 파일 정리."""
    result, note = process_one(job)
    repo = _jobs()
    if result == "done":
        repo.finish(job.id, note)
    elif result == "skipped":
        repo.finish(job.id, None)                # 중복은 성공으로 마감(재시도 무의미)
    else:
        # 파일을 못 읽는 종류의 실패는 다시 해도 같으므로 재시도하지 않는다
        retry = "읽지 못했습니다" not in note and "대기 파일이 없습니다" not in note
        repo.fail(job.id, note, retry=retry)
    if result in ("done", "skipped"):
        discard_staged(job.path)                 # 등록됐으면 대기 파일은 지운다
    print(f"  [{result}] {job.source_filename}"
          + (f" — {note}" if result != "done" else ""), flush=True)
    return result


def drain(workers: int, window=None) -> dict[str, int]:
    """대기열이 빌 때까지(또는 시간대가 끝날 때까지) 처리한다.

    시간대가 끝나면 **새 작업을 더 집지 않고**, 이미 손댄 문서만 마치고 멈춘다.
    문서는 한 건씩 커밋되므로 여기서 멈춰도 지금까지 등록한 것은 그대로 남고,
    다음 시간대에 남은 것부터 이어서 처리한다.
    """
    stats = {"done": 0, "skipped": 0, "failed": 0, "paused": 0}
    lock = threading.Lock()

    def open_now() -> bool:
        if window is None:
            return True
        return in_window(now_local(settings), *window)

    def run():
        while not _stop.is_set():
            was_open = open_now()
            job = _jobs().claim(window_open=was_open)
            if job is None:
                if not was_open:
                    with lock:
                        stats["paused"] += 1      # 시간대가 끝나 멈춤(남은 건 다음에)
                return
            kind = _handle(job)
            with lock:
                stats[kind] = stats.get(kind, 0) + 1

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for _ in range(max(1, workers)):
            pool.submit(run)
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="예약 업로드 처리 워커")
    ap.add_argument("--now", action="store_true", help="처리 시간대를 무시하고 지금 처리")
    ap.add_argument("--once", action="store_true", help="대기열을 한 번 비우고 종료")
    ap.add_argument("--workers", type=int, default=settings.ingest_workers)
    args = ap.parse_args(argv)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: _stop.set())

    start, end = window_from_settings(settings)
    window = None if args.now else (start, end)
    tz = tz_of(settings)
    print(f"예약 업로드 워커 시작 — 처리 시간대 {describe(start, end)} [{tz}]"
          f"{' (무시)' if args.now else ''} · 동시 {args.workers}", flush=True)
    print(f"  현재 시각 {now_local(settings):%Y-%m-%d %H:%M} ({tz})", flush=True)

    while not _stop.is_set():
        beat(settings)          # 살아 있음을 남긴다(화면·진단이 이걸로 판단)
        now = now_local(settings)
        if window is not None and not in_window(now, start, end):
            # 시간대 밖 — '지금 바로'로 올린 것만 처리하고, 나머지는 다음 시작까지 기다린다
            urgent = drain(args.workers, window=None) if _urgent_waiting() else None
            if urgent and urgent["done"] + urgent["failed"]:
                print(f"  즉시 처리 {urgent['done']}건 등록 · 실패 {urgent['failed']}",
                      flush=True)
            wait = min(seconds_until(now, start, settings), 300)  # 최대 5분마다 재확인
            if _jobs().counts()[UploadJobRepository.QUEUED]:
                print(f"  대기 중 — {start:%H:%M} 부터 처리합니다.", flush=True)
            if args.once:
                break
            _stop.wait(wait)
            continue

        reclaimed = _jobs().reclaim_stale(STALE_MINUTES)
        if reclaimed:
            print(f"  멈춰 있던 작업 {reclaimed}건을 대기열로 되돌렸습니다.", flush=True)

        # counts 는 '예약 시각이 아직 안 된 건'까지 세므로, 지금 집을 수 있는 것만 본다
        pending = _jobs().claimable()
        if pending:
            print(f"대기 {pending}건 처리 시작", flush=True)
            began = time.monotonic()
            stats = drain(args.workers, window=window)
            took = time.monotonic() - began
            print(f"→ 등록 {stats['done']} · 건너뜀 {stats['skipped']} · "
                  f"실패 {stats['failed']} · {took / 60:.1f}분", flush=True)
            left = _jobs().counts()[UploadJobRepository.QUEUED]
            if stats.get("paused") and left:
                print(f"  {end:%H:%M} 이 되어 멈춥니다 — 남은 {left}건은 "
                      f"{start:%H:%M} 부터 이어서 처리합니다.", flush=True)
        if args.once:
            break
        _stop.wait(IDLE_SLEEP)

    print("워커를 종료합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
