from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _local_timezone(timezone_name: str | None = None) -> timezone | ZoneInfo:
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def local_day_window_utc(
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> tuple[datetime, datetime]:
    tzinfo = _local_timezone(timezone_name)
    current = now or datetime.now(tzinfo)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tzinfo)
    local_now = current.astimezone(tzinfo)
    local_start = datetime.combine(local_now.date(), time.min, tzinfo=tzinfo)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)
