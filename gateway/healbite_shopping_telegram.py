from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Callable

from gateway.healbite_feature_gates import FeatureAvailabilityStatus
from gateway.healbite_shopping import (
    ShoppingItemOrigin,
    ShoppingItemOverrideState,
    ShoppingListView,
)
from gateway.healbite_shopping_runtime import (
    HealBiteShoppingRuntimeService,
    ShoppingRuntimeCleanupError,
    ShoppingRuntimeConflictError,
    ShoppingRuntimeNotFoundError,
    ShoppingRuntimeSourceError,
    ShoppingRuntimeStateError,
    ShoppingRuntimeUnavailableError,
    build_shopping_runtime_service,
)
from gateway.healbite_shopping_schema import require_shopping_item_id
from gateway.healbite_weekly_menu_schema import is_valid_week_start
from gateway.healbite_weekly_menu_telegram import current_week_start

SHOPPING_COMMAND = "/shopping"
SHOPPING_ADD_COMMAND = "/shopping_add"
SHOPPING_CALLBACK_ROOT = "shopping:"
SHOPPING_CALLBACK_PREFIX = "shopping:v1:"
SHOPPING_MAX_CALLBACK_BYTES = 64
SHOPPING_PAGE_SIZE = 8
SHOPPING_PLACEHOLDER_REPLY = "В разработке"
SHOPPING_UNAVAILABLE_REPLY = "Список покупок временно недоступен."
SHOPPING_ACTION_UNAVAILABLE_REPLY = "Список изменился. Откройте актуальную версию."
SHOPPING_ADD_HELP = (
    "Добавьте товар командой:\n/shopping_add Молоко\n/shopping_add Молоко | 2 | л"
)
SHOPPING_ADD_USAGE = "Формат: /shopping_add <название> [| количество | единица]"
SHOPPING_GENERATION_MISSING_MENU_REPLY = "На эту неделю меню ещё не создано."
SHOPPING_GENERATION_FAILED_REPLY = "Не удалось сформировать список по этому меню."
SHOPPING_GENERATION_SUCCESS_REPLY = "Список обновлён по недельному меню."
SHOPPING_GENERATION_CONFIRMATION = (
    "Обновить список покупок по недельному меню?\n\n"
    "Ручные позиции сохранятся. Позиции из меню будут пересчитаны. "
    "Удалённые позиции могут появиться снова, если они всё ещё нужны по меню. "
    "Недельное меню не изменится."
)

_PLACEHOLDER_STATES = {
    FeatureAvailabilityStatus.DISABLED,
    FeatureAvailabilityStatus.MISCONFIGURED,
    FeatureAvailabilityStatus.INVALID_ACTOR,
    FeatureAvailabilityStatus.NOT_ALLOWLISTED,
}
_MONTHS = {
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


@dataclass(frozen=True, slots=True)
class ShoppingTelegramScreen:
    text: str
    rows: tuple[tuple[tuple[str, str], ...], ...] = ()
    parse_mode: str | None = "HTML"


@dataclass(frozen=True, slots=True)
class ShoppingTelegramResult:
    state: str
    screen: ShoppingTelegramScreen
    notice: str | None = None
    error_class: str | None = None


@dataclass(frozen=True, slots=True)
class ShoppingCallback:
    action: str
    argument: str | None = None
    version: int | None = None
    desired_state: bool | None = None
    week_start: str | None = None


@dataclass(frozen=True, slots=True)
class ShoppingAddInput:
    name: str
    quantity: str | None
    unit: str


ShoppingRuntimeFactory = Callable[[], HealBiteShoppingRuntimeService]
NowFactory = Callable[[], datetime]


def _positive_actor(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        actor = int(value)
    except (TypeError, ValueError):
        return None
    return actor if 0 < actor <= 2**63 - 1 else None


def _week_label(week_start: str) -> str:
    start = date.fromisoformat(week_start)
    end = start + timedelta(days=6)
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {_MONTHS[end.month]}"
    return f"{start.day} {_MONTHS[start.month]} – {end.day} {_MONTHS[end.month]}"


def _safe_text(value: object, *, fallback: str, limit: int) -> str:
    collapsed = " ".join(str(value or "").split())
    if not collapsed:
        collapsed = fallback
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "…"
    return escape(collapsed)


def _callback(action: str, *parts: object) -> str:
    payload = ":".join([action, *(str(part) for part in parts)])
    data = f"{SHOPPING_CALLBACK_PREFIX}{payload}"
    if len(data.encode("utf-8")) > SHOPPING_MAX_CALLBACK_BYTES:
        raise ValueError("shopping callback is too long")
    return data


def _parse_positive_version(value: str) -> int | None:
    if not value.isdigit():
        return None
    parsed = int(value)
    return parsed if 0 < parsed <= 2**31 - 1 else None


def _week_token(week_start: str) -> str:
    if not is_valid_week_start(week_start):
        raise ValueError("invalid shopping week")
    return week_start.replace("-", "")


def _parse_week_token(value: str) -> str | None:
    if len(value) != 8 or not value.isdigit():
        return None
    week_start = f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return week_start if is_valid_week_start(week_start) else None


def _parse_generation_version(value: str) -> int | None:
    if not value.isdigit():
        return None
    parsed = int(value)
    return parsed if 0 <= parsed <= 2**31 - 1 else None


def _generation_callback(action: str, week_start: str, version: int) -> str:
    return _callback(action, _week_token(week_start), version)


def parse_shopping_callback(data: object) -> ShoppingCallback | None:
    if not isinstance(data, str):
        return None
    try:
        encoded_size = len(data.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if encoded_size > SHOPPING_MAX_CALLBACK_BYTES:
        return None
    if not data.startswith(SHOPPING_CALLBACK_PREFIX):
        return None
    parts = data[len(SHOPPING_CALLBACK_PREFIX) :].split(":")
    action = parts[0] if parts else ""
    if action in {"r", "a", "b", "cx"} and len(parts) == 1:
        return ShoppingCallback(action)
    if (
        action == "p"
        and len(parts) == 2
        and parts[1].isdigit()
        and int(parts[1]) <= 10000
    ):
        return ShoppingCallback(action, argument=parts[1])
    if action in {"cr", "cc"} and len(parts) == 2:
        version = _parse_positive_version(parts[1])
        return None if version is None else ShoppingCallback(action, version=version)
    if action in {"gr", "gc", "gx"} and len(parts) == 3:
        week_start = _parse_week_token(parts[1])
        version = _parse_generation_version(parts[2])
        if week_start is None or version is None:
            return None
        return ShoppingCallback(
            action,
            version=version,
            week_start=week_start,
        )
    if action == "d" and len(parts) == 3:
        version = _parse_positive_version(parts[2])
        if version is None:
            return None
        try:
            item_id = require_shopping_item_id(parts[1])
        except (TypeError, ValueError):
            return None
        return ShoppingCallback(action, argument=item_id, version=version)
    if action == "t" and len(parts) == 4 and parts[3] in {"0", "1"}:
        version = _parse_positive_version(parts[2])
        if version is None:
            return None
        try:
            item_id = require_shopping_item_id(parts[1])
        except (TypeError, ValueError):
            return None
        return ShoppingCallback(
            action,
            argument=item_id,
            version=version,
            desired_state=parts[3] == "1",
        )
    return None


def shopping_delivery_idempotency_key(delivery_id: object, *, operation: str) -> str:
    normalized = str(delivery_id or "").strip()
    digest = hashlib.sha256(f"{operation}:{normalized}".encode("utf-8")).hexdigest()
    return f"telegram-shopping:{digest}"


def parse_shopping_add_command(text: object) -> ShoppingAddInput | None:
    raw = str(text or "").strip()
    command, separator, arguments = raw.partition(" ")
    if command.split("@", 1)[0].lower() != SHOPPING_ADD_COMMAND or not separator:
        return None
    parts = [part.strip() for part in arguments.split("|")]
    if not parts or len(parts) > 3 or not parts[0]:
        return None
    if len(parts) == 1:
        return ShoppingAddInput(parts[0], None, "unknown")
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return ShoppingAddInput(parts[0], parts[1], parts[2].lower())


class HealBiteShoppingTelegramController:
    def __init__(
        self,
        *,
        runtime_factory: ShoppingRuntimeFactory = build_shopping_runtime_service,
        now_factory: NowFactory | None = None,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))

    def _runtime_for_actor(
        self,
        actor_user_id: object,
    ) -> tuple[int, HealBiteShoppingRuntimeService] | ShoppingTelegramResult:
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
    def _placeholder() -> ShoppingTelegramResult:
        return ShoppingTelegramResult(
            state="disabled",
            screen=ShoppingTelegramScreen(SHOPPING_PLACEHOLDER_REPLY, parse_mode=None),
        )

    @staticmethod
    def _unavailable(*, error_class: str = "unavailable") -> ShoppingTelegramResult:
        return ShoppingTelegramResult(
            state="unavailable",
            screen=ShoppingTelegramScreen(
                SHOPPING_UNAVAILABLE_REPLY,
                rows=((("Обновить", _callback("r")),),),
                parse_mode=None,
            ),
            error_class=error_class,
        )

    def _week_start(self) -> str:
        return current_week_start(now=self._now_factory(), timezone_name="UTC")

    def home(
        self,
        actor_user_id: object,
        *,
        page: int = 0,
        notice: str | None = None,
    ) -> ShoppingTelegramResult:
        resolved = self._runtime_for_actor(actor_user_id)
        if isinstance(resolved, ShoppingTelegramResult):
            return resolved
        actor, runtime = resolved
        week_start = self._week_start()
        try:
            view = runtime.get_current_shopping_list(actor, week_start)
        except ShoppingRuntimeUnavailableError as exc:
            if exc.availability.status in _PLACEHOLDER_STATES:
                return self._placeholder()
            return self._unavailable(error_class=exc.availability.status.value)
        except (ShoppingRuntimeCleanupError, ShoppingRuntimeStateError, sqlite3.Error):
            return self._unavailable(error_class="state_unavailable")
        except Exception:
            return self._unavailable(error_class="internal_error")
        if view is None:
            lines = [
                "<b>Список покупок</b>",
                _week_label(week_start),
                "",
            ]
            if notice:
                lines.extend([escape(notice), ""])
            lines.append("Список на эту неделю пока не создан.")
            return ShoppingTelegramResult(
                state="empty",
                screen=ShoppingTelegramScreen(
                    "\n".join(lines),
                    rows=(
                        (("Сформировать по меню", _generation_callback("gr", week_start, 0)),),
                        (("Обновить", _callback("r")),),
                        (("Назад", _callback("b")),),
                    ),
                ),
                notice=notice,
            )
        return self._render_view(view, page=page, notice=notice)

    def _render_view(
        self,
        view: ShoppingListView,
        *,
        page: int,
        notice: str | None,
    ) -> ShoppingTelegramResult:
        items = view.items
        last_page = max(0, (len(items) - 1) // SHOPPING_PAGE_SIZE)
        selected_page = max(0, min(int(page), last_page))
        start = selected_page * SHOPPING_PAGE_SIZE
        visible = items[start : start + SHOPPING_PAGE_SIZE]
        lines = [
            "<b>Список покупок</b>",
            _week_label(view.shopping_list.week_start),
            "",
        ]
        if notice:
            lines.extend([escape(notice), ""])
        if not visible:
            lines.append("Список пуст.")
        rows: list[tuple[tuple[str, str], ...]] = []
        for index, item in enumerate(visible, start=start + 1):
            marker = "✅" if item.checked_state else "▫️"
            name = _safe_text(item.display_name, fallback="Товар", limit=200)
            quantity = ""
            if item.quantity_value is not None:
                unit = _safe_text(item.quantity_unit_display, fallback="", limit=32)
                quantity = f" — {escape(item.quantity_value)}"
                if unit:
                    quantity = f"{quantity} {unit}"
            lines.append(f"{marker} {index}. {name}{quantity}")
            rows.append((
                (
                    "Вернуть" if item.checked_state else "Куплено",
                    _callback(
                        "t", item.id, item.version, 0 if item.checked_state else 1
                    ),
                ),
                ("Удалить", _callback("d", item.id, item.version)),
            ))
        paging: list[tuple[str, str]] = []
        if selected_page > 0:
            paging.append(("Назад", _callback("p", selected_page - 1)))
        if selected_page < last_page:
            paging.append(("Далее", _callback("p", selected_page + 1)))
        if paging:
            rows.append(tuple(paging))
        has_generated_items = any(
            item.origin is ShoppingItemOrigin.MENU_GENERATED
            and item.override_state is ShoppingItemOverrideState.NONE
            for item in items
        )
        generation_label = (
            "Обновить по меню"
            if has_generated_items
            else "Сформировать по меню"
        )
        rows.extend([
            ((
                generation_label,
                _generation_callback(
                    "gr",
                    view.shopping_list.week_start,
                    view.shopping_list.version,
                ),
            ),),
            (
                ("Добавить", _callback("a")),
                ("Обновить", _callback("r")),
            ),
            (("Очистить", _callback("cr", view.shopping_list.version)),),
            (("Назад", _callback("b")),),
        ])
        return ShoppingTelegramResult(
            state="home",
            screen=ShoppingTelegramScreen("\n".join(lines), rows=tuple(rows)),
        )

    def add_from_command(
        self,
        actor_user_id: object,
        command_text: object,
        *,
        delivery_id: object,
    ) -> ShoppingTelegramResult:
        resolved = self._runtime_for_actor(actor_user_id)
        if isinstance(resolved, ShoppingTelegramResult):
            return resolved
        actor, runtime = resolved
        parsed = parse_shopping_add_command(command_text)
        if parsed is None:
            return ShoppingTelegramResult(
                state="invalid_input",
                screen=ShoppingTelegramScreen(SHOPPING_ADD_USAGE, parse_mode=None),
                error_class="validation",
            )
        week_start = self._week_start()
        try:
            current = runtime.get_current_shopping_list(actor, week_start)
            if current is None:
                return self.home(actor)
            key = shopping_delivery_idempotency_key(delivery_id, operation="add")
            runtime.add_manual_shopping_item(
                actor,
                week_start,
                parsed.name,
                parsed.quantity,
                parsed.unit,
                key,
                current.shopping_list.version,
            )
            return self.home(actor, notice="Товар добавлен.")
        except (ShoppingRuntimeNotFoundError, ShoppingRuntimeStateError):
            return self.home(actor, notice=SHOPPING_ACTION_UNAVAILABLE_REPLY)
        except (
            ShoppingRuntimeUnavailableError,
            ShoppingRuntimeCleanupError,
            sqlite3.Error,
        ):
            return self._unavailable(error_class="state_unavailable")
        except Exception:
            return self._unavailable(error_class="internal_error")

    def handle_callback(
        self,
        actor_user_id: object,
        callback_data: object,
        *,
        callback_query_id: object,
    ) -> ShoppingTelegramResult:
        resolved = self._runtime_for_actor(actor_user_id)
        if isinstance(resolved, ShoppingTelegramResult):
            return resolved
        actor, runtime = resolved
        parsed = parse_shopping_callback(callback_data)
        if parsed is None:
            return ShoppingTelegramResult(
                state="stale",
                screen=ShoppingTelegramScreen(
                    SHOPPING_ACTION_UNAVAILABLE_REPLY,
                    rows=((("Обновить", _callback("r")),),),
                    parse_mode=None,
                ),
                error_class="invalid_callback",
            )
        if parsed.action in {"r", "p"}:
            return self.home(actor, page=int(parsed.argument or 0))
        if parsed.action == "a":
            return ShoppingTelegramResult(
                state="add_help",
                screen=ShoppingTelegramScreen(
                    SHOPPING_ADD_HELP,
                    rows=(
                        (("К списку", _callback("r")),),
                        (("Назад", _callback("b")),),
                    ),
                    parse_mode=None,
                ),
            )
        if parsed.action == "b":
            return ShoppingTelegramResult(
                state="back",
                screen=ShoppingTelegramScreen("", parse_mode=None),
            )
        if parsed.action == "cx":
            return self.home(actor)
        if parsed.action == "gx":
            return self.home(actor)
        if parsed.action == "gr":
            return self._generation_confirmation(runtime, actor, parsed)
        week_start = self._week_start()
        if parsed.action == "cr":
            current = self._safe_current(runtime, actor, week_start)
            if current is None:
                return self.home(actor, notice=SHOPPING_ACTION_UNAVAILABLE_REPLY)
            if current.shopping_list.version != parsed.version:
                return self.home(actor, notice=SHOPPING_ACTION_UNAVAILABLE_REPLY)
            return ShoppingTelegramResult(
                state="clear_confirmation",
                screen=ShoppingTelegramScreen(
                    "Очистить весь список покупок?",
                    rows=(
                        (("Да, очистить", _callback("cc", parsed.version)),),
                        (("Отмена", _callback("cx")),),
                    ),
                    parse_mode=None,
                ),
            )
        if parsed.action == "gc":
            return self._confirm_generation(
                runtime,
                actor,
                parsed,
                callback_query_id=callback_query_id,
            )
        try:
            key = shopping_delivery_idempotency_key(
                callback_query_id,
                operation=parsed.action,
            )
            if parsed.action == "cc":
                if parsed.version is None:
                    return self._unavailable(error_class="invalid_callback")
                runtime.clear_shopping_list(
                    actor,
                    week_start,
                    "all_items",
                    key,
                    parsed.version,
                )
                return self.home(actor, notice="Список очищен.")
            if parsed.argument is None or parsed.version is None:
                return self._unavailable(error_class="invalid_callback")
            if parsed.action == "t":
                if parsed.desired_state is None:
                    return self._unavailable(error_class="invalid_callback")
                runtime.set_shopping_item_checked(
                    actor,
                    parsed.argument,
                    parsed.desired_state,
                    key,
                    parsed.version,
                )
                return self.home(actor, notice="Статус обновлён.")
            if parsed.action == "d":
                runtime.delete_shopping_item(
                    actor,
                    parsed.argument,
                    key,
                    parsed.version,
                )
                return self.home(actor, notice="Товар удалён.")
        except (ShoppingRuntimeNotFoundError, ShoppingRuntimeStateError):
            return self.home(actor, notice=SHOPPING_ACTION_UNAVAILABLE_REPLY)
        except (
            ShoppingRuntimeUnavailableError,
            ShoppingRuntimeCleanupError,
            sqlite3.Error,
        ):
            return self._unavailable(error_class="state_unavailable")
        except Exception:
            return self._unavailable(error_class="internal_error")
        return self._unavailable(error_class="invalid_callback")

    def _generation_confirmation(
        self,
        runtime: HealBiteShoppingRuntimeService,
        actor: int,
        parsed: ShoppingCallback,
    ) -> ShoppingTelegramResult:
        week_start = self._week_start()
        if parsed.week_start != week_start or parsed.version is None:
            return self.home(actor, notice=SHOPPING_ACTION_UNAVAILABLE_REPLY)
        try:
            current = runtime.get_current_shopping_list(actor, week_start)
        except ShoppingRuntimeUnavailableError as exc:
            if exc.availability.status in _PLACEHOLDER_STATES:
                return self._placeholder()
            return self._unavailable(error_class=exc.availability.status.value)
        except (ShoppingRuntimeCleanupError, ShoppingRuntimeStateError, sqlite3.Error):
            return self._unavailable(error_class="state_unavailable")
        except Exception:
            return self._unavailable(error_class="internal_error")
        current_version = 0 if current is None else current.shopping_list.version
        if current_version != parsed.version:
            return self.home(actor, notice=SHOPPING_ACTION_UNAVAILABLE_REPLY)
        confirm_label = (
            "Да, сформировать"
            if current is None
            else "Да, обновить"
        )
        return ShoppingTelegramResult(
            state="generation_confirmation",
            screen=ShoppingTelegramScreen(
                SHOPPING_GENERATION_CONFIRMATION,
                rows=(
                    ((
                        confirm_label,
                        _generation_callback("gc", week_start, current_version),
                    ),),
                    ((
                        "Отмена",
                        _generation_callback("gx", week_start, current_version),
                    ),),
                ),
                parse_mode=None,
            ),
        )

    def _confirm_generation(
        self,
        runtime: HealBiteShoppingRuntimeService,
        actor: int,
        parsed: ShoppingCallback,
        *,
        callback_query_id: object,
    ) -> ShoppingTelegramResult:
        week_start = self._week_start()
        if parsed.week_start != week_start or parsed.version is None:
            return self.home(actor, notice=SHOPPING_ACTION_UNAVAILABLE_REPLY)
        try:
            generated = runtime.generate_shopping_list_from_weekly_menu(
                actor,
                week_start,
                shopping_delivery_idempotency_key(
                    callback_query_id,
                    operation="generate",
                ),
                parsed.version or None,
            )
        except ShoppingRuntimeNotFoundError:
            return self.home(actor, notice=SHOPPING_GENERATION_MISSING_MENU_REPLY)
        except ShoppingRuntimeConflictError:
            return self.home(actor, notice=SHOPPING_ACTION_UNAVAILABLE_REPLY)
        except ShoppingRuntimeSourceError:
            return self.home(actor, notice=SHOPPING_GENERATION_FAILED_REPLY)
        except ShoppingRuntimeUnavailableError as exc:
            if exc.availability.status in _PLACEHOLDER_STATES:
                return self._placeholder()
            return self._unavailable(error_class=exc.availability.status.value)
        except (ShoppingRuntimeCleanupError, sqlite3.Error):
            return self._unavailable(error_class="state_unavailable")
        except ShoppingRuntimeStateError:
            return self.home(actor, notice=SHOPPING_GENERATION_FAILED_REPLY)
        except Exception:
            return self._unavailable(error_class="internal_error")
        return self._render_view(
            generated,
            page=0,
            notice=SHOPPING_GENERATION_SUCCESS_REPLY,
        )

    @staticmethod
    def _safe_current(
        runtime: HealBiteShoppingRuntimeService,
        actor: int,
        week_start: str,
    ) -> ShoppingListView | None:
        try:
            return runtime.get_current_shopping_list(actor, week_start)
        except Exception:
            return None


def build_shopping_telegram_controller(
    *,
    runtime_factory: ShoppingRuntimeFactory | None = None,
    now_factory: NowFactory | None = None,
    env: dict[str, str] | None = None,
    db_path: str | Path | None = None,
) -> HealBiteShoppingTelegramController:
    if runtime_factory is None:
        runtime_factory = lambda: build_shopping_runtime_service(
            env=env, db_path=db_path
        )
    return HealBiteShoppingTelegramController(
        runtime_factory=runtime_factory,
        now_factory=now_factory,
    )
