from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from gateway.healbite_weight_reminders import (
    ReminderDeliveryState,
    ReminderSchedulingError,
    WeightReminderSetting,
    calculate_next_occurrence_utc,
    load_weight_reminder_config,
    validate_timezone,
)

SUPPORTED_WEIGHT_REMINDER_TIMEZONES: dict[str, tuple[str, str]] = {
    "utc": ("UTC", "UTC"),
    "moscow": ("Москва", "Europe/Moscow"),
    "berlin": ("Берлин", "Europe/Berlin"),
    "london": ("Лондон", "Europe/London"),
    "kyiv": ("Киев", "Europe/Kyiv"),
    "almaty": ("Алматы", "Asia/Almaty"),
    "tbilisi": ("Тбилиси", "Asia/Tbilisi"),
    "yerevan": ("Ереван", "Asia/Yerevan"),
    "dubai": ("Дубай", "Asia/Dubai"),
    "tashkent": ("Ташкент", "Asia/Tashkent"),
    "bishkek": ("Бишкек", "Asia/Bishkek"),
    "new_york": ("Нью-Йорк", "America/New_York"),
    "chicago": ("Чикаго", "America/Chicago"),
    "denver": ("Денвер", "America/Denver"),
    "los_angeles": ("Лос-Анджелес", "America/Los_Angeles"),
}

WEEKDAY_LABELS = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье",
}
REMINDER_MINUTES = ("00", "15", "30", "45")
REMINDER_HOURS = tuple(f"{hour:02d}" for hour in range(24))
DEFAULT_REMINDER_WEEKDAY = 6
DEFAULT_REMINDER_TIME = "09:00"
DEFAULT_REMINDER_TIMEZONE = "UTC"
WEIGHT_REMINDER_DRAFT_TTL = timedelta(minutes=30)


@dataclass(slots=True)
class WeightReminderDraft:
    mode: str
    step: str
    timezone_name: str | None = None
    weekday: int | None = None
    hour: str | None = None
    minute: str | None = None
    source_schedule_version: int | None = None
    updated_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def touch(self, *, now_utc: datetime | None = None) -> None:
        self.updated_at_utc = now_utc or datetime.now(timezone.utc)

    def expired(self, *, now_utc: datetime | None = None) -> bool:
        current = now_utc or datetime.now(timezone.utc)
        return current - self.updated_at_utc > WEIGHT_REMINDER_DRAFT_TTL

    @property
    def local_time(self) -> str | None:
        if self.hour is None or self.minute is None:
            return None
        return f"{self.hour}:{self.minute}"

    @property
    def complete(self) -> bool:
        return self.timezone_name is not None and self.weekday is not None and self.local_time is not None


def reminder_ui_enabled_for_user(user_id: int, *, env: dict[str, str] | None = None) -> bool:
    config = load_weight_reminder_config(env)
    return bool(config.enabled and int(user_id) in config.allowlist)


def supported_timezone_alias_for_name(timezone_name: str | None) -> str | None:
    normalized = (timezone_name or "").strip()
    for alias, (_label, tz_name) in SUPPORTED_WEIGHT_REMINDER_TIMEZONES.items():
        if tz_name == normalized:
            return alias
    return None


def timezone_name_for_alias(alias: str) -> str | None:
    entry = SUPPORTED_WEIGHT_REMINDER_TIMEZONES.get((alias or "").strip().lower())
    return entry[1] if entry else None


def timezone_label_for_name(timezone_name: str | None) -> str:
    normalized = (timezone_name or "").strip()
    for _alias, (label, tz_name) in SUPPORTED_WEIGHT_REMINDER_TIMEZONES.items():
        if tz_name == normalized:
            return f"{label} ({tz_name})" if label != tz_name else tz_name
    return normalized or "UTC"


def timezone_region_bucket(timezone_name: str | None) -> str:
    raw = (timezone_name or "").strip()
    if raw == "UTC":
        return "utc"
    region = raw.split("/", 1)[0].lower() if "/" in raw else "other"
    if region in {"europe", "asia", "america"}:
        return region
    return "other"


def time_bucket(local_time: str | None) -> str:
    try:
        hour = int((local_time or "").split(":", 1)[0])
    except (TypeError, ValueError):
        return "unknown"
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 18:
        return "day"
    if 18 <= hour < 23:
        return "evening"
    return "night"


def new_draft_from_setting(setting: WeightReminderSetting | None, *, mode: str) -> WeightReminderDraft:
    timezone_name = setting.timezone_name if setting is not None else DEFAULT_REMINDER_TIMEZONE
    weekday = setting.weekday if setting is not None else DEFAULT_REMINDER_WEEKDAY
    local_time = setting.local_time if setting is not None else DEFAULT_REMINDER_TIME
    hour, minute = local_time.split(":", 1)
    return WeightReminderDraft(
        mode=mode,
        step="timezone",
        timezone_name=timezone_name,
        weekday=int(weekday),
        hour=hour,
        minute=minute,
        source_schedule_version=setting.schedule_version if setting is not None else None,
    )


def draft_from_profile_timezone(setting: WeightReminderSetting | None, profile_timezone: str | None, *, mode: str) -> WeightReminderDraft:
    draft = new_draft_from_setting(setting, mode=mode)
    if setting is None and supported_timezone_alias_for_name(profile_timezone):
        draft.timezone_name = profile_timezone
    return draft


def validate_draft(draft: WeightReminderDraft) -> tuple[str, int, str] | None:
    if not draft.complete or draft.weekday is None:
        return None
    try:
        timezone_name = validate_timezone(draft.timezone_name or "")
    except ReminderSchedulingError:
        return None
    if draft.weekday < 0 or draft.weekday > 6:
        return None
    local_time = draft.local_time
    if local_time is None:
        return None
    hour, minute = local_time.split(":", 1)
    if hour not in REMINDER_HOURS or minute not in REMINDER_MINUTES:
        return None
    return timezone_name, int(draft.weekday), local_time


def rows(*items: tuple[str, str] | list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    result: list[list[tuple[str, str]]] = []
    for item in items:
        if isinstance(item, list):
            result.append(item)
        else:
            result.append([item])
    return result


def main_screen(setting: WeightReminderSetting | None) -> tuple[str, list[list[tuple[str, str]]]]:
    title = "Напоминание о взвешивании"
    if setting is None:
        return (
            f"{title}\n\nСтатус: выключено\nРасписание ещё не настроено.",
            rows(("Настроить и включить", "weight:reminder:start"), ("Назад", "weight:refresh")),
        )
    day = WEEKDAY_LABELS.get(setting.weekday, "—")
    text = (
        f"{title}\n\n"
        f"Статус: {'включено' if setting.enabled and setting.delivery_state is ReminderDeliveryState.ACTIVE else 'выключено'}\n"
        f"День: {day}\n"
        f"Время: {setting.local_time}\n"
        f"Часовой пояс: {html.escape(setting.timezone_name)}"
    )
    if setting.delivery_state is ReminderDeliveryState.SUSPENDED:
        text = (
            f"{title}\n\n"
            f"Статус: приостановлено\n"
            f"День: {day}\n"
            f"Время: {setting.local_time}\n"
            f"Часовой пояс: {html.escape(setting.timezone_name)}\n\n"
            "Доставка была приостановлена. Вы можете включить её снова."
        )
        return text, rows(
            ("Включить снова", "weight:reminder:resume"),
            ("Изменить расписание", "weight:reminder:edit"),
            ("Отключить", "weight:reminder:disable"),
            ("Назад", "weight:refresh"),
        )
    if setting.enabled:
        return text, rows(
            ("Изменить расписание", "weight:reminder:edit"),
            ("Отключить", "weight:reminder:disable"),
            ("Назад", "weight:refresh"),
        )
    return text, rows(("Настроить и включить", "weight:reminder:edit"), ("Назад", "weight:refresh"))


def timezone_screen(draft: WeightReminderDraft) -> tuple[str, list[list[tuple[str, str]]]]:
    text = "Выберите часовой пояс для напоминания."
    buttons: list[list[tuple[str, str]]] = []
    items = list(SUPPORTED_WEIGHT_REMINDER_TIMEZONES.items())
    for index in range(0, len(items), 2):
        row = []
        for alias, (label, _tz) in items[index:index + 2]:
            row.append((label, f"weight:reminder:tz:{alias}"))
        buttons.append(row)
    buttons.append([("Отмена", "weight:reminder:cancel")])
    return text, buttons


def weekday_screen(draft: WeightReminderDraft) -> tuple[str, list[list[tuple[str, str]]]]:
    buttons = [[(label, f"weight:reminder:day:{day}")] for day, label in WEEKDAY_LABELS.items()]
    buttons.append([("Назад", "weight:reminder:back"), ("Отмена", "weight:reminder:cancel")])
    return "Выберите день недели.", buttons


def hour_screen(draft: WeightReminderDraft) -> tuple[str, list[list[tuple[str, str]]]]:
    buttons: list[list[tuple[str, str]]] = []
    for index in range(0, 24, 4):
        buttons.append([(hour, f"weight:reminder:hour:{hour}") for hour in REMINDER_HOURS[index:index + 4]])
    buttons.append([("Назад", "weight:reminder:back"), ("Отмена", "weight:reminder:cancel")])
    return "Выберите час.", buttons


def minute_screen(draft: WeightReminderDraft) -> tuple[str, list[list[tuple[str, str]]]]:
    buttons = [[(minute, f"weight:reminder:minute:{minute}") for minute in REMINDER_MINUTES]]
    buttons.append([("Назад", "weight:reminder:back"), ("Отмена", "weight:reminder:cancel")])
    return "Выберите минуты.", buttons


def review_screen(draft: WeightReminderDraft) -> tuple[str, list[list[tuple[str, str]]]]:
    parsed = validate_draft(draft)
    if parsed is None:
        return "Эта настройка больше не актуальна. Откройте экран ещё раз.", rows(("Назад", "weight:reminder"))
    timezone_name, weekday, local_time = parsed
    text = (
        "Проверьте напоминание перед включением.\n\n"
        f"День: {WEEKDAY_LABELS.get(weekday, '—')}\n"
        f"Время: {local_time}\n"
        f"Часовой пояс: {html.escape(timezone_name)}"
    )
    action_label = "Сохранить" if draft.source_schedule_version is not None else "Включить"
    return text, rows(
        (action_label, "weight:reminder:confirm"),
        [
            ("Изменить день", "weight:reminder:weekday"),
            ("Изменить время", "weight:reminder:time"),
        ],
        ("Изменить часовой пояс", "weight:reminder:timezone"),
        ("Отмена", "weight:reminder:cancel"),
    )


def disable_screen(setting: WeightReminderSetting | None) -> tuple[str, list[list[tuple[str, str]]]]:
    return "Отключить напоминание?", rows(("Отключить", "weight:reminder:disable_confirm"), ("Назад", "weight:reminder"))


def resume_screen(setting: WeightReminderSetting | None) -> tuple[str, list[list[tuple[str, str]]]]:
    if setting is None:
        return "Эта настройка больше не актуальна. Откройте экран ещё раз.", rows(("Назад", "weight:reminder"))
    text = (
        "Включить напоминание снова?\n\n"
        f"День: {WEEKDAY_LABELS.get(setting.weekday, '—')}\n"
        f"Время: {setting.local_time}\n"
        f"Часовой пояс: {html.escape(setting.timezone_name)}"
    )
    return text, rows(("Включить снова", "weight:reminder:resume_confirm"), ("Назад", "weight:reminder"))


def callback_payloads() -> Iterable[str]:
    yield "weight:reminder"
    for action in ("start", "timezone", "weekday", "time", "review", "confirm", "edit", "disable", "disable_confirm", "resume", "resume_confirm", "cancel", "back"):
        yield f"weight:reminder:{action}"
    for alias in SUPPORTED_WEIGHT_REMINDER_TIMEZONES:
        yield f"weight:reminder:tz:{alias}"
    for day in range(7):
        yield f"weight:reminder:day:{day}"
    for hour in REMINDER_HOURS:
        yield f"weight:reminder:hour:{hour}"
    for minute in REMINDER_MINUTES:
        yield f"weight:reminder:minute:{minute}"


def preview_next_due(timezone_name: str, weekday: int, local_time: str, *, now_utc: datetime | None = None) -> str:
    occurrence = calculate_next_occurrence_utc(now_utc or datetime.now(timezone.utc), timezone_name, weekday, local_time)
    return occurrence.scheduled_utc.strftime("%Y-%m-%d %H:%M:%S")
