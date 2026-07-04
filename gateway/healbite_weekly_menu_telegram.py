from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from time import monotonic
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gateway.healbite_feature_gates import FeatureAvailabilityStatus
from gateway.healbite_weekly_menu_runtime import (
    HealBiteWeeklyMenuRuntimeService,
    WeeklyMenuRuntimeCleanupError,
    WeeklyMenuRuntimeStateError,
    WeeklyMenuRuntimeUnavailableError,
)
from gateway.healbite_weekly_menus import WeeklyMenuRevisionView

WEEKLY_MENU_COMMAND = "/weekly_menu"
WEEKLY_MENU_PLACEHOLDER_REPLY = "В разработке"
WEEKLY_MENU_UNAVAILABLE_REPLY = "Функция временно недоступна. Попробуйте позже."
WEEKLY_MENU_EMPTY_REPLY = "Меню на эту неделю пока не составлено."
WEEKLY_MENU_DEFAULT_TIMEZONE = "UTC"
WEEKLY_MENU_PARSE_MODE = "HTML"
WEEKLY_MENU_MAX_CHUNK_LENGTH = 3500
WEEKLY_MENU_MAX_ENTRY_TITLE_LENGTH = 240

_RUSSIAN_MONTHS = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}
_RUSSIAN_WEEKDAYS = (
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
)
_MEAL_SLOT_LABELS = {
    "breakfast": "Завтрак",
    "lunch": "Обед",
    "dinner": "Ужин",
    "snack": "Перекус",
}
_MEAL_SLOT_ORDER = ("breakfast", "lunch", "dinner", "snack")
_PLACEHOLDER_STATES = {
    FeatureAvailabilityStatus.DISABLED,
    FeatureAvailabilityStatus.MISCONFIGURED,
    FeatureAvailabilityStatus.INVALID_ACTOR,
    FeatureAvailabilityStatus.NOT_ALLOWLISTED,
}


@dataclass(frozen=True, slots=True)
class WeeklyMenuTelegramPresentation:
    state: str
    chunks: tuple[str, ...]
    parse_mode: str | None
    week_start: str | None = None
    timezone_name: str = WEEKLY_MENU_DEFAULT_TIMEZONE
    entry_count: int = 0
    duration_ms: int = 0


def _normalize_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _safe_timezone_name(timezone_name: str | None) -> str:
    candidate = str(timezone_name or "").strip() or WEEKLY_MENU_DEFAULT_TIMEZONE
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        return WEEKLY_MENU_DEFAULT_TIMEZONE
    return candidate


def current_week_start(
    *,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> str:
    normalized_now = _normalize_now(now)
    zone = ZoneInfo(_safe_timezone_name(timezone_name))
    local_today = normalized_now.astimezone(zone).date()
    monday = local_today - timedelta(days=local_today.weekday())
    return monday.isoformat()


def _date_range_label(week_start: str) -> str:
    start = date.fromisoformat(week_start)
    end = start + timedelta(days=6)
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {_RUSSIAN_MONTHS[end.month]}"
    if start.year == end.year:
        return f"{start.day} {_RUSSIAN_MONTHS[start.month]} — {end.day} {_RUSSIAN_MONTHS[end.month]}"
    return (
        f"{start.day} {_RUSSIAN_MONTHS[start.month]} {start.year} — "
        f"{end.day} {_RUSSIAN_MONTHS[end.month]} {end.year}"
    )


def _day_label(day: date) -> str:
    return f"{_RUSSIAN_WEEKDAYS[day.weekday()]}, {day.day} {_RUSSIAN_MONTHS[day.month]}"


def _safe_entry_title(value: object) -> str:
    collapsed = " ".join(str(value or "").split())
    if len(collapsed) > WEEKLY_MENU_MAX_ENTRY_TITLE_LENGTH:
        collapsed = collapsed[: WEEKLY_MENU_MAX_ENTRY_TITLE_LENGTH - 1].rstrip()
        collapsed = f"{collapsed}…"
    return escape(collapsed or "Блюдо")


def _render_day_block(view: WeeklyMenuRevisionView, *, day: date) -> str:
    lines = [f"<b>{_day_label(day)}</b>"]
    day_entries = [entry for entry in view.entries if entry.local_date == day.isoformat()]
    if not day_entries:
        lines.append("—")
        return "\n".join(lines)

    for meal_slot in _MEAL_SLOT_ORDER:
        slot_entries = [entry for entry in day_entries if entry.meal_slot.value == meal_slot]
        if not slot_entries:
            continue
        label = _MEAL_SLOT_LABELS.get(meal_slot, meal_slot)
        if len(slot_entries) == 1:
            lines.append(f"{label}: {_safe_entry_title(slot_entries[0].title)}")
            continue
        lines.append(f"{label}:")
        for index, entry in enumerate(slot_entries, start=1):
            lines.append(f"{index}. {_safe_entry_title(entry.title)}")
    return "\n".join(lines)


def _header_lines(week_start: str) -> list[str]:
    return [
        "<b>📋 Меню на неделю</b>",
        _date_range_label(week_start),
    ]


def _split_long_block(block: str, *, max_length: int) -> list[str]:
    if len(block) <= max_length:
        return [block]
    parts: list[str] = []
    current_lines: list[str] = []
    current_length = 0
    for line in block.splitlines():
        if len(line) > max_length:
            line = f"{line[: max(1, max_length - 1)].rstrip()}…"
        line_length = len(line) + (1 if current_lines else 0)
        if current_lines and current_length + line_length > max_length:
            parts.append("\n".join(current_lines))
            current_lines = [line]
            current_length = len(line)
            continue
        current_lines.append(line)
        current_length += line_length
    if current_lines:
        parts.append("\n".join(current_lines))
    return parts or [block[:WEEKLY_MENU_MAX_CHUNK_LENGTH]]


def chunk_weekly_menu_text(
    *,
    week_start: str,
    day_blocks: list[str],
) -> tuple[str, ...]:
    header = "\n".join(_header_lines(week_start))
    block_limit_with_header = WEEKLY_MENU_MAX_CHUNK_LENGTH - len(header) - 2
    chunks: list[str] = []
    current = header
    first_chunk = True
    for block in day_blocks:
        block_parts = _split_long_block(
            block,
            max_length=block_limit_with_header if first_chunk else WEEKLY_MENU_MAX_CHUNK_LENGTH,
        )
        for block_part in block_parts:
            addition = f"\n\n{block_part}" if current else block_part
            if len(current) + len(addition) > WEEKLY_MENU_MAX_CHUNK_LENGTH and current:
                chunks.append(current)
                current = ""
                first_chunk = False
                addition = block_part
            if len(current) + len(addition) > WEEKLY_MENU_MAX_CHUNK_LENGTH and not current:
                chunks.append(block_part)
                continue
            current += addition
    if current:
        chunks.append(current)
    elif not chunks:
        chunks.append(header)
    return tuple(chunks)


def render_weekly_menu(view: WeeklyMenuRevisionView) -> tuple[str, ...]:
    week_start = view.series.week_start
    start = date.fromisoformat(week_start)
    day_blocks = [_render_day_block(view, day=start + timedelta(days=offset)) for offset in range(7)]
    return chunk_weekly_menu_text(week_start=week_start, day_blocks=day_blocks)


def resolve_weekly_menu_presentation(
    *,
    actor_user_id: object,
    runtime_service: HealBiteWeeklyMenuRuntimeService,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> WeeklyMenuTelegramPresentation:
    started = monotonic()
    safe_timezone = _safe_timezone_name(timezone_name)
    week_start = current_week_start(now=now, timezone_name=safe_timezone)
    try:
        view = runtime_service.get_active_published_weekly_menu_for_week(actor_user_id, week_start)
    except WeeklyMenuRuntimeUnavailableError as exc:
        text = (
            WEEKLY_MENU_PLACEHOLDER_REPLY
            if exc.availability.status in _PLACEHOLDER_STATES
            else WEEKLY_MENU_UNAVAILABLE_REPLY
        )
        state = "placeholder" if exc.availability.status in _PLACEHOLDER_STATES else "unavailable"
        return WeeklyMenuTelegramPresentation(
            state=state,
            chunks=(text,),
            parse_mode=None,
            week_start=week_start,
            timezone_name=safe_timezone,
            duration_ms=int((monotonic() - started) * 1000),
        )
    except (WeeklyMenuRuntimeCleanupError, WeeklyMenuRuntimeStateError):
        return WeeklyMenuTelegramPresentation(
            state="unavailable",
            chunks=(WEEKLY_MENU_UNAVAILABLE_REPLY,),
            parse_mode=None,
            week_start=week_start,
            timezone_name=safe_timezone,
            duration_ms=int((monotonic() - started) * 1000),
        )
    if view is None:
        return WeeklyMenuTelegramPresentation(
            state="empty",
            chunks=(WEEKLY_MENU_EMPTY_REPLY,),
            parse_mode=None,
            week_start=week_start,
            timezone_name=safe_timezone,
            duration_ms=int((monotonic() - started) * 1000),
        )

    chunks = render_weekly_menu(view)
    return WeeklyMenuTelegramPresentation(
        state="published",
        chunks=chunks,
        parse_mode=WEEKLY_MENU_PARSE_MODE,
        week_start=view.series.week_start,
        timezone_name=safe_timezone,
        entry_count=len(view.entries),
        duration_ms=int((monotonic() - started) * 1000),
    )


def build_weekly_menu_presentation_for_now(
    *,
    actor_user_id: object,
    runtime_factory: Callable[[], HealBiteWeeklyMenuRuntimeService],
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> WeeklyMenuTelegramPresentation:
    runtime_service = runtime_factory()
    return resolve_weekly_menu_presentation(
        actor_user_id=actor_user_id,
        runtime_service=runtime_service,
        now=now,
        timezone_name=timezone_name,
    )
