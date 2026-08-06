"""처리 시간대(업무시간을 피해 야간에만 돌리기) 계산.

예약 업로드는 GPU 를 오래 쓰므로 업무 시간에 돌리면 채팅 응답이 느려진다.
기본은 18:00~08:00(자정을 넘김). 설정은 `INGEST_WINDOW_START/END`.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Optional


def parse_hhmm(value: str, default: time) -> time:
    try:
        hh, _, mm = str(value).partition(":")
        return time(int(hh), int(mm or 0))
    except (TypeError, ValueError):
        return default


def in_window(now: datetime, start: time, end: time) -> bool:
    """지금이 처리 시간대 안인가. start>end 면 자정을 넘기는 구간으로 본다."""
    cur = now.time()
    if start == end:
        return True                     # 같으면 24시간 허용
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end    # 예: 18:00~08:00


def seconds_until(now: datetime, start: time) -> float:
    """다음 시작 시각까지 남은 초."""
    today = now.replace(hour=start.hour, minute=start.minute,
                        second=0, microsecond=0)
    if today <= now:
        today += timedelta(days=1)
    return max(1.0, (today - now).total_seconds())


def window_from_settings(settings) -> tuple[time, time]:
    return (parse_hhmm(getattr(settings, "ingest_window_start", "18:00"), time(18, 0)),
            parse_hhmm(getattr(settings, "ingest_window_end", "08:00"), time(8, 0)))


def describe(start: time, end: time) -> str:
    return f"{start:%H:%M}~{end:%H:%M}" + ("(익일)" if start > end else "")


def next_run_hint(now: datetime, start: time, end: time) -> Optional[str]:
    """화면 안내용: 지금 처리 중인지, 아니면 언제 시작하는지."""
    if in_window(now, start, end):
        return None
    secs = seconds_until(now, start)
    hours, mins = int(secs // 3600), int(secs % 3600 // 60)
    when = f"{hours}시간 {mins}분 뒤" if hours else f"{mins}분 뒤"
    return f"{start:%H:%M}부터 처리를 시작합니다({when})."
