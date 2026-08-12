"""예약 업로드가 안 될 때 원인을 콕 집어 준다.

    python -m scripts.check_queue

예약이 '대기 중'에서 안 넘어가는 원인은 셋뿐이다.
  ① **워커가 안 돌고 있다** — 제일 흔하다. 띄워 두지 않으면 파일만 쌓인다.
  ② 예약 시작 시각이 아직 안 됐다 — 정상이다. 그 시각까지 기다리면 된다.
  ③ 처리 시간대(18:00~08:00) 밖이다 — '지금 바로'로 올린 것만 처리된다.
"""

from __future__ import annotations

import sys

from app.config import settings
from app.db.repositories import UploadJobRepository
from app.manage.schedule import (HEARTBEAT_STALE_MIN, describe, heartbeat_path,
                                 in_window, now_local, to_local, tz_of,
                                 window_from_settings, worker_status)


def main(argv=None) -> int:
    from app.review.factory import new_session

    start, end = window_from_settings(settings)
    now = now_local(settings)
    inside = in_window(now, start, end)
    wk = worker_status(settings)

    print("── 워커 ─────────────────────────────────────────────────")
    if wk["alive"]:
        print(f"  ✅ 살아 있음 (마지막 신호 {wk['minutes_ago']}분 전)")
    elif wk["last_beat"] is None:
        print("  ❌ **한 번도 돈 적이 없습니다** — 워커를 띄우지 않았습니다.")
        print(f"     (신호 파일 없음: {heartbeat_path(settings)})")
    else:
        print(f"  ❌ **멈춰 있습니다** — 마지막 신호가 {wk['minutes_ago']}분 전입니다"
              f"(기준 {HEARTBEAT_STALE_MIN}분).")

    print("\n── 시간 ─────────────────────────────────────────────────")
    print(f"  지금            : {now:%Y-%m-%d %H:%M} ({tz_of(settings)})")
    print(f"  처리 시간대     : {describe(start, end)}  → 지금 "
          f"{'안에 있음' if inside else '밖에 있음'}")

    try:
        session = new_session()
    except Exception as e:      # DB 에 못 붙으면 여기서 끝난다 — 그것 자체가 원인이다
        print("\n── 대기열 ───────────────────────────────────────────────")
        print(f"  ❌ **데이터베이스에 붙지 못했습니다** — {type(e).__name__}: {e}")
        print("     예약 업로드는 DB 를 쓰므로 이 상태로는 아무것도 처리되지 않습니다.")
        print("     .env 의 DATABASE_URL 과 Postgres 컨테이너 상태를 확인하세요.")
        return 1

    try:
        repo = UploadJobRepository(session)
        counts = repo.counts()
        ready = repo.claimable(window_open=inside)
        print("\n── 대기열 ───────────────────────────────────────────────")
        print(f"  대기 {counts[repo.QUEUED]} · 처리 중 {counts[repo.PROCESSING]} · "
              f"완료 {counts[repo.DONE]} · 실패 {counts[repo.FAILED]}")
        print(f"  지금 처리 가능한 건: {ready}")

        waiting = repo.list_jobs(status=repo.QUEUED, limit=10)
        if waiting:
            print("\n  대기 중인 것(최근 10건):")
            for j in waiting:
                when = j.get("start_after")
                label = (f"{to_local(when, settings):%m-%d %H:%M} 부터"
                         if when else "시작 시각 없음(바로 가능)")
                mark = "지금 바로" if j.get("bypass_window") else label
                print(f"    · {j['filename'][:34]:34s} {mark}")
    finally:
        session.close()

    print("\n── 판정 ─────────────────────────────────────────────────")
    if not wk["alive"]:
        print("  ▶ **워커가 안 돌고 있는 것이 원인입니다.** 아래로 띄우세요.")
        print("      python -m scripts.ingest_worker            # 임시로 확인")
        print("      sudo systemctl enable --now hr-ingest-worker   # 상시 (권장)")
        print("    떠 있어야 예약이 처리됩니다. docs/예약업로드.md 참고.")
    elif ready == 0 and counts[UploadJobRepository.QUEUED] > 0:
        if not inside:
            print(f"  ▶ 처리 시간대 밖입니다. {start:%H:%M} 부터 처리됩니다.")
            print("    (지금 당장 돌리려면: python -m scripts.ingest_worker --now --once)")
        else:
            print("  ▶ 예약 시작 시각이 아직 안 됐습니다. 위 목록의 시각을 확인하세요.")
    elif counts[UploadJobRepository.QUEUED] == 0:
        print("  ▶ 대기 중인 예약이 없습니다. 정상입니다.")
    else:
        print("  ▶ 워커도 살아 있고 처리 가능한 건도 있습니다. 곧 처리됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
