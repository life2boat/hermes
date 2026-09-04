from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from time import monotonic
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gateway.healbite_feature_gates import FeatureAvailabilityStatus
from gateway.healbite_weekly_menu_runtime import (
    HealBiteWeeklyMenuRuntimeService,
    WeeklyMenuRuntimeCleanupError,
    WeeklyMenuRuntimeStateError,
    WeeklyMenuRuntimeUnavailableError,
    build_weekly_menu_runtime_service,
)
from gateway.healbite_weekly_menus import (
    WeeklyMenuRevisionStatus,
    WeeklyMenuRevisionView,
)

WEEKLY_MENU_COMMAND = "/weekly_menu"
WEEKLY_MENU_CALLBACK_ROOT = "weekly_menu:"
WEEKLY_MENU_CALLBACK_PREFIX = "weekly_menu:v1:"
WEEKLY_MENU_MAX_CALLBACK_BYTES = 64
WEEKLY_MENU_PLACEHOLDER_REPLY = "В разработке"
WEEKLY_MENU_UNAVAILABLE_REPLY = "Функция временно недоступна. Попробуйте позже."
WEEKLY_MENU_ACTION_UNAVAILABLE_REPLY = "Меню изменилось. Откройте актуальную версию."
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


@dataclass(frozen=True, slots=True)
class WeeklyMenuTelegramScreen:
    text: str
    rows: tuple[tuple[tuple[str, str], ...], ...] = ()
    parse_mode: str | None = "HTML"


@dataclass(frozen=True, slots=True)
class WeeklyMenuTelegramResult:
    state: str
    screen: WeeklyMenuTelegramScreen
    notice: str | None = None
    error_class: str | None = None
    entry_count: int = 0


@dataclass(frozen=True, slots=True)
class WeeklyMenuCallback:
    action: str
    week_start: str | None = None
    series_version: int | None = None
    revision_version: int | None = None


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


def _positive_actor(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        actor = int(value)
    except (TypeError, ValueError):
        return None
    return actor if 0 < actor <= 2**63 - 1 else None


def _week_token(week_start: str) -> str:
    from gateway.healbite_weekly_menu_schema import is_valid_week_start

    if not is_valid_week_start(week_start):
        raise ValueError("invalid weekly menu week")
    return week_start.replace("-", "")


def _parse_week_token(value: str) -> str | None:
    from gateway.healbite_weekly_menu_schema import is_valid_week_start

    if len(value) != 8 or not value.isdigit():
        return None
    week_start = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return week_start if is_valid_week_start(week_start) else None


def _callback(action: str, *parts: object) -> str:
    payload = ":".join([action, *(str(part) for part in parts)])
    data = f"{WEEKLY_MENU_CALLBACK_PREFIX}{payload}"
    if len(data.encode("utf-8")) > WEEKLY_MENU_MAX_CALLBACK_BYTES:
        raise ValueError("weekly menu callback is too long")
    return data


def parse_weekly_menu_callback(data: object) -> WeeklyMenuCallback | None:
    if not isinstance(data, str):
        return None
    try:
        encoded_size = len(data.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if encoded_size > WEEKLY_MENU_MAX_CALLBACK_BYTES:
        return None
    if not data.startswith(WEEKLY_MENU_CALLBACK_PREFIX):
        return None
    parts = data[len(WEEKLY_MENU_CALLBACK_PREFIX) :].split(":")
    action = parts[0] if parts else ""
    if action in {"b", "r", "sh"} and len(parts) == 1:
        return WeeklyMenuCallback(action=action)
    if action == "g" and len(parts) == 2:
        week_start = _parse_week_token(parts[1])
        if week_start is None:
            return None
        return WeeklyMenuCallback(action="g", week_start=week_start)
    if action == "pub" and len(parts) == 4:
        week_start = _parse_week_token(parts[1])
        if week_start is None:
            return None
        if not parts[2].isdigit() or not parts[3].isdigit():
            return None
        return WeeklyMenuCallback(
            action="pub",
            week_start=week_start,
            series_version=int(parts[2]),
            revision_version=int(parts[3]),
        )
    return None


def render_draft_weekly_menu(
    view: WeeklyMenuRevisionView,
    *,
    notice: str | None = None,
) -> str:
    week_start = view.series.week_start
    start = date.fromisoformat(week_start)
    day_blocks = [_render_day_block(view, day=start + timedelta(days=offset)) for offset in range(7)]
    header_parts = [
        "<b>📋 Черновик меню на неделю</b>",
        _date_range_label(week_start),
    ]
    if notice:
        header_parts.extend(["", f"<b>{escape(notice)}</b>"])
    blocks = ["\n".join(header_parts)]
    blocks.extend(day_blocks)
    blocks.append("<i>Черновик ещё не опубликован. Вы можете одобрить его или создать заново.</i>")
    return "\n\n".join(blocks)


def render_published_weekly_menu(
    view: WeeklyMenuRevisionView,
    *,
    notice: str | None = None,
) -> str:
    chunks = render_weekly_menu(view)
    text = chunks[0]
    if notice:
        text = f"<b>{escape(notice)}</b>\n\n{text}"
    return text


def render_empty_weekly_menu(
    week_start: str,
    *,
    notice: str | None = None,
) -> str:
    lines = [
        "<b>📋 Меню на неделю</b>",
        _date_range_label(week_start),
        "",
    ]
    if notice:
        lines.extend([f"<b>{escape(notice)}</b>", ""])
    lines.append("Меню на эту неделю пока не создано.")
    return "\n".join(lines)


class HealBiteWeeklyMenuTelegramController:
    def __init__(
        self,
        *,
        runtime_factory: Callable[[], HealBiteWeeklyMenuRuntimeService] | None = None,
        mutation_factory: Callable[[], object] | None = None,
        generation_service_factory: Callable[[], object] | None = None,
        shopping_runtime_factory: Callable[[], object] | None = None,
        inventory_store_factory: Callable[[], object] | None = None,
        now_factory: Callable[[], datetime] | None = None,
        timezone_name: str = WEEKLY_MENU_DEFAULT_TIMEZONE,
        db_path: str | Path | None = None,
    ) -> None:
        self._db_path = db_path
        self._runtime_factory = runtime_factory or (
            lambda: build_weekly_menu_runtime_service(db_path=self._db_path)
        )
        self._mutation_factory = mutation_factory or self._default_mutation_service
        self._generation_service_factory = (
            generation_service_factory or self._default_generation_service
        )
        self._shopping_runtime_factory = shopping_runtime_factory or self._default_shopping_runtime
        self._inventory_store_factory = inventory_store_factory or self._default_inventory_store
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._timezone_name = timezone_name

    def _default_mutation_service(self):
        from gateway.healbite_weekly_menu_mutation_runtime import (
            HealBiteWeeklyMenuMutationRuntimeService,
        )
        runtime = self._runtime_factory()
        cfg = getattr(runtime, "_config", None)
        return HealBiteWeeklyMenuMutationRuntimeService(db_path=self._db_path, config=cfg)

    def _default_generation_service(self):
        from gateway.healbite_weekly_menu_generation import (
            AuxiliaryWeeklyMenuGenerator,
            CanonicalWeeklyMenuMemberSnapshotProvider,
            HealBiteWeeklyMenuGenerationService,
        )
        runtime = self._runtime_factory()
        cfg = getattr(runtime, "_config", None)
        return HealBiteWeeklyMenuGenerationService(
            generator=AuxiliaryWeeklyMenuGenerator(),
            member_snapshot_provider=CanonicalWeeklyMenuMemberSnapshotProvider(
                db_path=self._db_path
            ),
            config=cfg,
            db_path=self._db_path,
        )

    def _default_shopping_runtime(self):
        from gateway.healbite_shopping_runtime import HealBiteShoppingRuntimeService

        runtime = self._runtime_factory()
        cfg = getattr(runtime, "_config", None)
        return HealBiteShoppingRuntimeService(config=cfg, db_path=self._db_path)

    def _default_inventory_store(self):
        from gateway.healbite_inventory import HealBiteInventoryStore

        return HealBiteInventoryStore(db_path=self._db_path)

    def _resolve_inventory_scope(self, actor: int):
        from gateway.healbite_households import (
            HealBiteHouseholdService,
            HealBiteHouseholdStore,
        )
        from gateway.healbite_inventory import InventoryOwnerScope

        context = HealBiteHouseholdService(
            HealBiteHouseholdStore(db_path=self._db_path, ensure_schema_on_init=False)
        ).resolve_existing_actor_household_context(actor)
        return InventoryOwnerScope(household_id=context.household_id)

    def _runtime_for_actor(
        self,
        actor_user_id: object,
    ) -> tuple[int, HealBiteWeeklyMenuRuntimeService] | WeeklyMenuTelegramResult:
        actor = _positive_actor(actor_user_id)
        if actor is None:
            return self._placeholder()
        runtime = self._runtime_factory()
        availability = runtime.get_availability(actor)
        if not availability.ready:
            if availability.status in _PLACEHOLDER_STATES:
                return self._placeholder()
            return self._unavailable(error_class=availability.status.value)
        return actor, runtime

    @staticmethod
    def _placeholder() -> WeeklyMenuTelegramResult:
        return WeeklyMenuTelegramResult(
            state="disabled",
            screen=WeeklyMenuTelegramScreen(WEEKLY_MENU_PLACEHOLDER_REPLY, parse_mode=None),
        )

    @staticmethod
    def _unavailable(*, error_class: str = "unavailable") -> WeeklyMenuTelegramResult:
        return WeeklyMenuTelegramResult(
            state="unavailable",
            screen=WeeklyMenuTelegramScreen(
                WEEKLY_MENU_UNAVAILABLE_REPLY,
                rows=((("Обновить", _callback("r")),),),
                parse_mode=None,
            ),
            error_class=error_class,
        )

    def _week_start(self) -> str:
        return current_week_start(now=self._now_factory(), timezone_name=self._timezone_name)

    def home(
        self,
        actor_user_id: object,
        *,
        notice: str | None = None,
    ) -> WeeklyMenuTelegramResult:
        resolved = self._runtime_for_actor(actor_user_id)
        if isinstance(resolved, WeeklyMenuTelegramResult):
            return resolved
        actor, runtime = resolved
        week_start = self._week_start()
        try:
            published_view = runtime.get_active_published_weekly_menu_for_week(actor, week_start)
            week_view = (
                runtime.get_weekly_menu_for_week(actor, week_start)
                if published_view is None
                else None
            )
        except WeeklyMenuRuntimeUnavailableError as exc:
            if exc.availability.status in _PLACEHOLDER_STATES:
                return self._placeholder()
            return self._unavailable(error_class=exc.availability.status.value)
        except (WeeklyMenuRuntimeCleanupError, WeeklyMenuRuntimeStateError, sqlite3.Error):
            return self._unavailable(error_class="state_unavailable")
        except Exception:
            return self._unavailable(error_class="internal_error")

        if published_view is not None:
            text = render_published_weekly_menu(published_view, notice=notice)
            rows = (
                (("🔄 Создать заново", _callback("g", _week_token(week_start))),),
                (("🛒 Список покупок", _callback("sh")),),
                (("⬅️ Назад", _callback("b")),),
            )
            return WeeklyMenuTelegramResult(
                state="published",
                screen=WeeklyMenuTelegramScreen(
                    text, rows=rows, parse_mode=WEEKLY_MENU_PARSE_MODE
                ),
                notice=notice,
                entry_count=len(published_view.entries),
            )

        if week_view is not None:
            draft = next(
                (r for r in week_view.revisions if r.status is WeeklyMenuRevisionStatus.DRAFT),
                None,
            )
            if draft is not None:
                try:
                    draft_view = runtime.get_weekly_menu_revision(actor, draft.id)
                except Exception:
                    draft_view = None
                if draft_view is not None:
                    text = render_draft_weekly_menu(draft_view, notice=notice)
                    rows = (
                        ((
                            "✅ Одобрить и сохранить",
                            _callback(
                                "pub",
                                _week_token(week_start),
                                week_view.series.version,
                                draft.version,
                            ),
                        ),),
                        (("🔄 Пересоздать", _callback("g", _week_token(week_start))),),
                        (("⬅️ Назад", _callback("b")),),
                    )
                    return WeeklyMenuTelegramResult(
                        state="draft",
                        screen=WeeklyMenuTelegramScreen(
                            text, rows=rows, parse_mode=WEEKLY_MENU_PARSE_MODE
                        ),
                        notice=notice,
                        entry_count=len(draft_view.entries),
                    )

        text = render_empty_weekly_menu(week_start, notice=notice)
        rows = (
            (("✨ Создать меню", _callback("g", _week_token(week_start))),),
            (("⬅️ Назад", _callback("b")),),
        )
        return WeeklyMenuTelegramResult(
            state="empty",
            screen=WeeklyMenuTelegramScreen(
                text, rows=rows, parse_mode=WEEKLY_MENU_PARSE_MODE
            ),
            notice=notice,
        )

    def handle_callback(
        self,
        actor_user_id: object,
        callback_data: object,
        *,
        callback_query_id: object = None,
    ) -> WeeklyMenuTelegramResult:
        resolved = self._runtime_for_actor(actor_user_id)
        if isinstance(resolved, WeeklyMenuTelegramResult):
            return resolved
        actor, runtime = resolved
        parsed = parse_weekly_menu_callback(callback_data)
        if parsed is None:
            return WeeklyMenuTelegramResult(
                state="stale",
                screen=WeeklyMenuTelegramScreen(
                    WEEKLY_MENU_ACTION_UNAVAILABLE_REPLY,
                    rows=((("Обновить", _callback("r")),),),
                    parse_mode=None,
                ),
                error_class="invalid_callback",
            )
        if parsed.action == "r":
            return self.home(actor)
        if parsed.action == "b":
            return WeeklyMenuTelegramResult(
                state="back",
                screen=WeeklyMenuTelegramScreen("", parse_mode=None),
            )
        if parsed.action == "sh":
            return WeeklyMenuTelegramResult(
                state="open_shopping",
                screen=WeeklyMenuTelegramScreen("", parse_mode=None),
            )
        week_start = parsed.week_start or self._week_start()
        if parsed.action == "g":
            inventory_snapshot_id: str | None = None
            try:
                scope = self._resolve_inventory_scope(actor)
                inv_store = self._inventory_store_factory()
                latest_inv = inv_store.get_latest_confirmed_snapshot(scope)
                if latest_inv is not None and latest_inv.items:
                    inventory_snapshot_id = latest_inv.snapshot.id
            except Exception:
                inventory_snapshot_id = None
            gen_key = f"telegram-weekly-gen:{actor}:{week_start}:{callback_query_id or int(monotonic() * 1000)}"
            try:
                gen_service = self._generation_service_factory()
                gen_result = gen_service.generate_draft_for_week(
                    actor,
                    week_start,
                    idempotency_key=gen_key,
                    inventory_snapshot_id=inventory_snapshot_id,
                )
            except Exception:
                return self.home(actor, notice="Не удалось создать меню. Попробуйте позже.")
            from gateway.healbite_weekly_menu_generation import WeeklyMenuGenerationStatus

            if gen_result.status != WeeklyMenuGenerationStatus.SUCCESS:
                if gen_result.status in {
                    WeeklyMenuGenerationStatus.DISABLED,
                    WeeklyMenuGenerationStatus.NOT_ALLOWLISTED,
                    WeeklyMenuGenerationStatus.INVALID_ACTOR,
                }:
                    return self._placeholder()
                return self.home(actor, notice="Не удалось создать меню. Попробуйте позже.")
            return self.home(actor, notice="Черновик меню создан!")

        if parsed.action == "pub":
            from gateway.healbite_weekly_menu_mutation_runtime import WeeklyMenuMutationStatus

            try:
                week_view = runtime.get_weekly_menu_for_week(actor, week_start)
                if week_view is None:
                    return self.home(actor, notice=WEEKLY_MENU_ACTION_UNAVAILABLE_REPLY)
                draft = next(
                    (r for r in week_view.revisions if r.status is WeeklyMenuRevisionStatus.DRAFT),
                    None,
                )
                if draft is None:
                    return self.home(actor, notice=WEEKLY_MENU_ACTION_UNAVAILABLE_REPLY)
            except Exception:
                return self._unavailable(error_class="state_unavailable")

            pub_key = f"telegram-weekly-pub:{actor}:{draft.id}:{callback_query_id or int(monotonic() * 1000)}"
            try:
                mutation_service = self._mutation_factory()
                series_ver = (
                    parsed.series_version
                    if parsed.series_version is not None
                    else week_view.series.version
                )
                revision_ver = (
                    parsed.revision_version
                    if parsed.revision_version is not None
                    else draft.version
                )
                pub_result = mutation_service.publish_draft(
                    actor,
                    draft.id,
                    expected_series_version=series_ver,
                    expected_revision_version=revision_ver,
                    idempotency_key=pub_key,
                )
            except Exception:
                return self.home(actor, notice="Не удалось опубликовать меню. Попробуйте позже.")

            if not pub_result.success:
                if pub_result.status in {
                    WeeklyMenuMutationStatus.DISABLED,
                    WeeklyMenuMutationStatus.NOT_ALLOWLISTED,
                    WeeklyMenuMutationStatus.INVALID_ACTOR,
                }:
                    return self._placeholder()
                return self.home(actor, notice=WEEKLY_MENU_ACTION_UNAVAILABLE_REPLY)

            # Auto derive shopping list from published menu
            notice = "Меню опубликовано!"
            try:
                shopping_runtime = self._shopping_runtime_factory()
                shop_key = f"weekly-shop-derivation:{actor}:{week_start}:{draft.id}"
                shopping_runtime.generate_shopping_list_from_weekly_menu(
                    actor,
                    week_start,
                    idempotency_key=shop_key,
                    expected_list_version=None,
                )
                notice = "Меню опубликовано! Список покупок сформирован."
            except Exception:
                pass
            return self.home(actor, notice=notice)

        return self.home(actor)


def build_weekly_menu_telegram_controller(
    *,
    runtime_factory: Callable[[], HealBiteWeeklyMenuRuntimeService] | None = None,
    mutation_factory: Callable[[], object] | None = None,
    generation_service_factory: Callable[[], object] | None = None,
    shopping_runtime_factory: Callable[[], object] | None = None,
    inventory_store_factory: Callable[[], object] | None = None,
    db_path: str | Path | None = None,
    now_factory: Callable[[], datetime] | None = None,
    timezone_name: str = WEEKLY_MENU_DEFAULT_TIMEZONE,
) -> HealBiteWeeklyMenuTelegramController:
    return HealBiteWeeklyMenuTelegramController(
        runtime_factory=runtime_factory,
        mutation_factory=mutation_factory,
        generation_service_factory=generation_service_factory,
        shopping_runtime_factory=shopping_runtime_factory,
        inventory_store_factory=inventory_store_factory,
        db_path=db_path,
        now_factory=now_factory,
        timezone_name=timezone_name,
    )
