"""처리 시간대(업무시간을 피해 야간에만 돌리기) 계산.

예약 업로드는 GPU 를 오래 쓰므로 업무 시간에 돌리면 채팅 응답이 느려진다.
기본은 18:00~08:00(자정을 넘김). 설정은 `INGEST_WINDOW_START/END`.

**시간대는 반드시 명시한다.** 서버 시스템 시계가 UTC 인 경우가 흔한데, `datetime.now()`
를 그대로 쓰면 "18:00" 이 18:00 UTC = 새벽 3시 KST 로 해석된다. 그러면 야간에 돌리려던
작업이 정확히 업무시간에 돌아간다. 그래서 여기서는 항상 `TZ`(기본 Asia/Seoul) 기준으로
계산하고, 저장·비교는 UTC 로 한다.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "Asia/Seoul"


def tz_of(settings=None) -> ZoneInfo:
    """설정된 업무 시간대. 이름이 잘못돼 있으면 기본값으로 떨어진다."""
    name = getattr(settings, "schedule_timezone", None) or DEFAULT_TZ
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def now_local(settings=None) -> datetime:
    """업무 시간대 기준 현재 시각(시스템 시계가 UTC 여도 안전)."""
    return datetime.now(tz_of(settings))


def to_local(moment: datetime, settings=None) -> datetime:
    """어떤 시각이든 업무 시간대로 변환(naive 는 UTC 로 간주)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz_of(settings))


def parse_hhmm(value: str, default: time) -> time:
    try:
        hh, _, mm = str(value).partition(":")
        hour, minute = int(hh), int(mm or 0)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return default
        return time(hour, minute)
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


def next_at(target: time, settings=None, after: Optional[datetime] = None) -> datetime:
    """업무 시간대 기준 '다음 target 시각'을 **UTC 로** 돌려준다.

    오늘 그 시각이 이미 지났으면 내일로 넘긴다. 예약 시작 시각 계산에 쓴다.
    """
    base = after or now_local(settings)
    base = to_local(base, settings)
    when = base.replace(hour=target.hour, minute=target.minute,
                        second=0, microsecond=0)
    if when <= base:
        when += timedelta(days=1)
    return when.astimezone(timezone.utc)


def seconds_until(now: datetime, start: time, settings=None) -> float:
    """다음 시작 시각까지 남은 초."""
    now = to_local(now, settings)
    return max(1.0, (next_at(start, settings, after=now) - now).total_seconds())


def window_from_settings(settings) -> tuple[time, time]:
    return (parse_hhmm(getattr(settings, "ingest_window_start", "18:00"), time(18, 0)),
            parse_hhmm(getattr(settings, "ingest_window_end", "08:00"), time(8, 0)))


def describe(start: time, end: time) -> str:
    return f"{start:%H:%M}~{end:%H:%M}" + ("(익일)" if start > end else "")


def next_run_hint(now: datetime, start: time, end: time, settings=None) -> Optional[str]:
    """화면 안내용: 지금 처리 중인지, 아니면 언제 시작하는지."""
    now = to_local(now, settings)
    if in_window(now, start, end):
        return None
    secs = seconds_until(now, start, settings)
    hours, mins = int(secs // 3600), int(secs % 3600 // 60)
    when = f"{hours}시간 {mins}분 뒤" if hours else f"{mins}분 뒤"
    return f"{start:%H:%M}부터 처리를 시작합니다({when})."
