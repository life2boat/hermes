from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE_NAME = "Europe/Moscow"

# Common Russian city to IANA timezone mappings
COMMON_CITY_TIMEZONES: dict[str, str] = {
    "москва": "Europe/Moscow",
    "санкт-петербург": "Europe/Moscow",
    "питер": "Europe/Moscow",
    "мск": "Europe/Moscow",
    "калининград": "Europe/Kaliningrad",
    "самара": "Europe/Samara",
    "екатеринбург": "Asia/Yekaterinburg",
    "уфа": "Asia/Yekaterinburg",
    "челябинск": "Asia/Yekaterinburg",
    "пермь": "Asia/Yekaterinburg",
    "тюмень": "Asia/Yekaterinburg",
    "омск": "Asia/Omsk",
    "новосибирск": "Asia/Novosibirsk",
    "томск": "Asia/Tomsk",
    "барнаул": "Asia/Barnaul",
    "кемерово": "Asia/Kemerovo",
    "красноярск": "Asia/Krasnoyarsk",
    "иркутск": "Asia/Irkutsk",
    "чита": "Asia/Chita",
    "якутск": "Asia/Yakutsk",
    "владивосток": "Asia/Vladivostok",
    "хабаровск": "Asia/Vladivostok",
    "магадан": "Asia/Magadan",
    "сахалин": "Asia/Sakhalin",
    "южно-сахалинск": "Asia/Sakhalin",
    "камчатка": "Asia/Kamchatka",
    "петропавловск-камчатский": "Asia/Kamchatka",
    "сочи": "Europe/Moscow",
    "казань": "Europe/Moscow",
    "нижний новгород": "Europe/Moscow",
    "ростов-на-дону": "Europe/Moscow",
    "краснодар": "Europe/Moscow",
    "волгоград": "Europe/Volgograd",
    "саратов": "Europe/Saratov",
    "астрахань": "Europe/Astrakhan",
    "ульяновск": "Europe/Ulyanovsk",
    "киров": "Europe/Kirov",
    "минск": "Europe/Minsk",
    "киев": "Europe/Kiev",
    "алматы": "Asia/Almaty",
    "астана": "Asia/Almaty",
    "ташкент": "Asia/Tashkent",
    "тбилиси": "Asia/Tbilisi",
    "ереван": "Asia/Yerevan",
    "баку": "Asia/Baku",
}

# Offset to primary representative IANA timezone mappings
OFFSET_TIMEZONES: dict[str, str] = {
    "+2": "Europe/Kaliningrad",
    "+02:00": "Europe/Kaliningrad",
    "utc+2": "Europe/Kaliningrad",
    "gmt+2": "Europe/Kaliningrad",
    "+3": "Europe/Moscow",
    "+03:00": "Europe/Moscow",
    "utc+3": "Europe/Moscow",
    "gmt+3": "Europe/Moscow",
    "msk": "Europe/Moscow",
    "+4": "Europe/Samara",
    "+04:00": "Europe/Samara",
    "utc+4": "Europe/Samara",
    "gmt+4": "Europe/Samara",
    "+5": "Asia/Yekaterinburg",
    "+05:00": "Asia/Yekaterinburg",
    "utc+5": "Asia/Yekaterinburg",
    "gmt+5": "Asia/Yekaterinburg",
    "+6": "Asia/Omsk",
    "+06:00": "Asia/Omsk",
    "utc+6": "Asia/Omsk",
    "gmt+6": "Asia/Omsk",
    "+7": "Asia/Novosibirsk",
    "+07:00": "Asia/Novosibirsk",
    "utc+7": "Asia/Novosibirsk",
    "gmt+7": "Asia/Novosibirsk",
    "+8": "Asia/Irkutsk",
    "+08:00": "Asia/Irkutsk",
    "utc+8": "Asia/Irkutsk",
    "gmt+8": "Asia/Irkutsk",
    "+9": "Asia/Yakutsk",
    "+09:00": "Asia/Yakutsk",
    "utc+9": "Asia/Yakutsk",
    "gmt+9": "Asia/Yakutsk",
    "+10": "Asia/Vladivostok",
    "+10:00": "Asia/Vladivostok",
    "utc+10": "Asia/Vladivostok",
    "gmt+10": "Asia/Vladivostok",
    "+11": "Asia/Magadan",
    "+11:00": "Asia/Magadan",
    "utc+11": "Asia/Magadan",
    "gmt+11": "Asia/Magadan",
    "+12": "Asia/Kamchatka",
    "+12:00": "Asia/Kamchatka",
    "utc+12": "Asia/Kamchatka",
    "gmt+12": "Asia/Kamchatka",
    "0": "UTC",
    "+0": "UTC",
    "utc": "UTC",
    "gmt": "UTC",
    "utc+0": "UTC",
    "utc-0": "UTC",
}


def normalize_timezone_name(value: str | None) -> str | None:
    """
    Normalizes a user-supplied timezone string, city name, or offset to a valid IANA timezone name.
    Returns None if the timezone cannot be resolved.
    """
    if not value:
        return None
    raw = str(value).strip()
    lowered = raw.lower()

    # 1. Direct city lookup
    if lowered in COMMON_CITY_TIMEZONES:
        return COMMON_CITY_TIMEZONES[lowered]

    # 2. Offset lookup
    clean_offset = re.sub(r"\s+", "", lowered)
    if clean_offset in OFFSET_TIMEZONES:
        return OFFSET_TIMEZONES[clean_offset]

    # 3. Direct ZoneInfo lookup
    try:
        ZoneInfo(raw)
        return raw
    except (ZoneInfoNotFoundError, ValueError):
        pass

    # 4. Case-insensitive / normalized lookup in common names
    for city, tz in COMMON_CITY_TIMEZONES.items():
        if city in lowered or lowered in city:
            return tz

    # 5. Check if it's formatted like Etc/GMT...
    try:
        ZoneInfo(raw.title())
        return raw.title()
    except (ZoneInfoNotFoundError, ValueError):
        pass

    return None


def validate_timezone(value: str | None) -> str:
    """
    Validates a timezone string. Returns the canonical IANA name or raises ValueError.
    """
    normalized = normalize_timezone_name(value)
    if normalized is None:
        raise ValueError(
            f"Недопустимый часовой пояс: «{value}». "
            "Укажите название города (например, Москва, Екатеринбург) "
            "или часовой пояс (например, Europe/Moscow, UTC+3)."
        )
    return normalized


def _local_timezone(timezone_name: str | None = None) -> timezone | ZoneInfo:
    if timezone_name:
        normalized = normalize_timezone_name(timezone_name)
        if normalized:
            try:
                return ZoneInfo(normalized)
            except ZoneInfoNotFoundError:
                pass
    return ZoneInfo(DEFAULT_TIMEZONE_NAME)


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


def format_local_now(
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> str:
    """Format current wall-clock time in specified timezone for UI confirmation."""
    tzinfo = _local_timezone(timezone_name)
    current = now or datetime.now(tzinfo)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tzinfo)
    local_time = current.astimezone(tzinfo)
    return local_time.strftime("%H:%M (%d.%m.%Y)")
