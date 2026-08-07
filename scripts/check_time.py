"""서버 시계·시간대 점검 — 예약 업로드 시각이 이상할 때 먼저 돌린다.

    python -m scripts.check_time

시각이 틀리는 원인은 둘 중 하나다.

  ① **시간대 설정이 틀림** — 이건 아래 출력으로 바로 안다.
  ② **서버 시계 자체가 틀림** — 프로그램은 알 수 없다. 출력된 시각을 손목시계·휴대폰과
     대조해야 한다. 시계가 어긋나 있으면 예약 시각도 그만큼 어긋난다.

②라면 서버에서 시간을 맞춰야 한다.

    timedatectl                       # 현재 상태 확인
    sudo timedatectl set-ntp true     # NTP 동기화(사내 NTP 서버가 있으면 그걸로)
    sudo timedatectl set-time '2026-08-07 10:02:00'   # NTP 를 못 쓰면 수동

도커로 띄웠다면 컨테이너 시계는 호스트를 따라간다 — 호스트를 먼저 맞춘다.
"""

from __future__ import annotations

import sys
from datetime import timedelta

from app.config import settings
from app.manage.schedule import (clock_report, describe, in_window, next_at,
                                 next_run_hint, window_from_settings)


def main(argv=None) -> int:
    rep = clock_report(settings)
    start, end = window_from_settings(settings)

    print("── 서버 시계 ────────────────────────────────────────────")
    print(f"  호스트                : {rep['host']}")
    print(f"  UTC                   : {rep['utc']:%Y-%m-%d %H:%M:%S}")
    print(f"  업무 시간대({rep['tz']}) : {rep['local']:%Y-%m-%d %H:%M:%S}  ← 화면에 뜨는 시각")
    print(f"  시간대 표기           : {rep['tz_label']}")
    print(f"  서버 시스템 로컬      : {rep['system_naive']:%Y-%m-%d %H:%M:%S} "
          f"({rep['system_tz'] or '이름 없음'})")

    print("\n── 예약 처리 ────────────────────────────────────────────")
    print(f"  처리 시간대           : {describe(start, end)}")
    inside = in_window(rep["local"], start, end)
    print(f"  지금 처리 시간대인가  : {'예' if inside else '아니오'}")
    nxt = next_at(start, settings)
    print(f"  다음 시작             : {nxt.astimezone(rep['local'].tzinfo):%m-%d %H:%M} "
          f"(UTC {nxt:%m-%d %H:%M})")
    hint = next_run_hint(rep["local"], start, end, settings)
    if hint:
        print(f"  안내 문구             : {hint}")

    print("\n── 확인 ─────────────────────────────────────────────────")
    print(f"  위 '업무 시간대' 시각({rep['local']:%H:%M})이 **지금 실제 시각과 같습니까?**")
    print("    같다  → 정상입니다.")
    print("    다르다 → 서버 시계가 어긋나 있습니다. 예약 시각도 그만큼 어긋납니다.")
    off = rep["local"].utcoffset() or timedelta(0)
    if off != timedelta(hours=9) and str(rep["tz"]) == "Asia/Seoul":
        print("  ⚠️ Asia/Seoul 인데 오프셋이 +09:00 이 아닙니다 — tzdata 를 확인하세요.")
    print("\n  시간대를 바꾸려면 .env 의 SCHEDULE_TIMEZONE 을 고치고 워커를 재시작하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
