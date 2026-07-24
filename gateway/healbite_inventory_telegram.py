from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Awaitable, Callable

from gateway.healbite_feature_gates import (
    FeatureGateConfig,
    evaluate_feature_gate,
    load_feature_gate_config,
)
from gateway.healbite_household_schema import HouseholdRole
from gateway.healbite_households import (
    HealBiteHouseholdService,
    HealBiteHouseholdStore,
    HouseholdAccessError,
    HouseholdIntegrityError,
    HouseholdNotFoundError,
    HouseholdValidationError,
)
from gateway.healbite_inventory import (
    HealBiteInventoryStore,
    InventoryAccessError,
    InventoryItemInput,
    InventoryNotFoundError,
    InventoryOwnerScope,
    InventorySnapshotView,
    InventoryStateError,
    InventoryValidationError,
    parse_inventory_text,
)
from gateway.healbite_weekly_menu_generation import (
    AuxiliaryWeeklyMenuGenerator,
    CanonicalWeeklyMenuMemberSnapshotProvider,
    HealBiteWeeklyMenuGenerationService,
    WeeklyMenuGenerationStatus,
)
from gateway.healbite_weekly_menu_telegram import (
    WEEKLY_MENU_MAX_CHUNK_LENGTH,
    WEEKLY_MENU_PARSE_MODE,
    current_week_start,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    WeeklyMenuRevisionStatus,
    WeeklyMenuRevisionView,
)

INVENTORY_COMMAND = "/inventory"
INVENTORY_CALLBACK_ROOT = "inventory:"
INVENTORY_CALLBACK_PREFIX = "inventory:v1:"
INVENTORY_MAX_CALLBACK_BYTES = 64
INVENTORY_PAGE_SIZE = 8
INVENTORY_MAX_ITEMS = 100
INVENTORY_MAX_GENERATION_ATTEMPTS = 2
INVENTORY_PLACEHOLDER_REPLY = "В разработке"
INVENTORY_UNAVAILABLE_REPLY = "Функция временно недоступна. Попробуйте позже."
INVENTORY_TEXT_PROMPT = "Отправьте список продуктов через запятую или с новой строки. Например: молоко 1 л, яйца 10 шт. /cancel — отменить."
INVENTORY_PHOTO_PROMPT = "Отправьте одно фото продуктов. Список будет показан для проверки и не будет сохранён без подтверждения."
INVENTORY_EDIT_PROMPT = (
    "Отправьте новое значение: название [количество] [единица]. /cancel — отменить."
)
INVENTORY_ADD_PROMPT = (
    "Отправьте один продукт: название [количество] [единица]. /cancel — отменить."
)
INVENTORY_EMPTY_REPLY = "Подтверждённого списка пока нет."


@dataclass(frozen=True, slots=True)
class InventoryTelegramScreen:
    text: str
    rows: tuple[tuple[tuple[str, str], ...], ...] = ()
    parse_mode: str | None = WEEKLY_MENU_PARSE_MODE


@dataclass(frozen=True, slots=True)
class InventoryTelegramResult:
    state: str
    screen: InventoryTelegramScreen
    notice: str | None = None
    error_class: str | None = None
    item_count: int = 0
    continuations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PendingInput:
    mode: str
    snapshot_id: str | None = None
    source_revision: int | None = None
    position: int | None = None


@dataclass(frozen=True, slots=True)
class _InventoryCallback:
    action: str
    snapshot_id: str | None = None
    source_revision: int | None = None
    position: int | None = None
    page: int = 0


VisionAnalyzeFn = Callable[[str, str], Awaitable[object]]


def _positive_actor(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        actor = int(value)
    except (TypeError, ValueError):
        return None
    return actor if actor > 0 else None


def _uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        return False


def parse_inventory_callback(value: object) -> _InventoryCallback | None:
    text = str(value or "")
    if len(text.encode("utf-8")) > INVENTORY_MAX_CALLBACK_BYTES:
        return None
    parts = text.split(":")
    if parts[:2] != ["inventory", "v1"] or len(parts) < 3:
        return None
    action = parts[2]
    if action in {"h", "t", "p", "l", "b"} and len(parts) == 3:
        return _InventoryCallback(action)
    if action == "r" and len(parts) == 6 and _uuid(parts[3]):
        try:
            return _InventoryCallback(
                action, parts[3], int(parts[4]), page=int(parts[5])
            )
        except ValueError:
            return None
    if (
        action in {"a", "c", "x", "g", "v", "rg", "ap"}
        and len(parts) == 5
        and _uuid(parts[3])
    ):
        try:
            return _InventoryCallback(action, parts[3], int(parts[4]))
        except ValueError:
            return None
    if action in {"e", "d"} and len(parts) == 6 and _uuid(parts[3]):
        try:
            return _InventoryCallback(action, parts[3], int(parts[4]), int(parts[5]))
        except ValueError:
            return None
    return None


class HealBiteInventoryTelegramController:
    def __init__(
        self,
        *,
        text_config: FeatureGateConfig | None = None,
        photo_config: FeatureGateConfig | None = None,
        weekly_generation_config: FeatureGateConfig | None = None,
        db_path: str | Path | None = None,
        vision_analyze_fn: VisionAnalyzeFn | None = None,
        generation_service_factory: Callable[[], HealBiteWeeklyMenuGenerationService]
        | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._text_config = text_config or load_feature_gate_config(
            "HEALBITE_INVENTORY_TEXT_UI"
        )
        self._photo_config = photo_config or load_feature_gate_config(
            "HEALBITE_INVENTORY_PHOTO_UI"
        )
        self._weekly_generation_config = (
            weekly_generation_config
            or load_feature_gate_config("HEALBITE_INVENTORY_WEEKLY_GENERATION_UI")
        )
        self._db_path = db_path
        self._vision_analyze_fn = vision_analyze_fn or self._default_vision_analyze
        self._generation_service_factory = (
            generation_service_factory or self._default_generation_service
        )
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._pending: dict[int, _PendingInput] = {}
        self._generation_attempts: dict[tuple[int, str, str], int] = {}
        self._generation_attempts_lock = threading.Lock()

    async def _default_vision_analyze(self, image_path: str, prompt: str) -> object:
        from tools.vision_tools import vision_analyze_tool

        return await vision_analyze_tool(image_path, prompt)

    def _default_generation_service(self) -> HealBiteWeeklyMenuGenerationService:
        return HealBiteWeeklyMenuGenerationService(
            generator=AuxiliaryWeeklyMenuGenerator(),
            member_snapshot_provider=CanonicalWeeklyMenuMemberSnapshotProvider(
                db_path=self._db_path
            ),
            db_path=self._db_path,
        )

    def _gate(self, kind: str, actor: object):
        return evaluate_feature_gate(
            {
                "text": self._text_config,
                "photo": self._photo_config,
                "weekly": self._weekly_generation_config,
            }[kind],
            actor,
        )

    def _resolve_scope(
        self, actor_user_id: object
    ) -> tuple[int, InventoryOwnerScope, object]:
        actor = _positive_actor(actor_user_id)
        if actor is None:
            raise HouseholdAccessError("invalid actor")
        context = HealBiteHouseholdService(
            HealBiteHouseholdStore(db_path=self._db_path, ensure_schema_on_init=False)
        ).resolve_existing_actor_household_context(actor)
        if context.role is not HouseholdRole.OWNER:
            raise HouseholdAccessError("household access denied")
        return actor, InventoryOwnerScope(household_id=context.household_id), context

    def _store(self) -> HealBiteInventoryStore:
        return HealBiteInventoryStore(db_path=self._db_path)

    @staticmethod
    def _result(
        state: str,
        text: str,
        *,
        rows=(),
        notice: str | None = None,
        error_class: str | None = None,
        item_count: int = 0,
        continuations: tuple[str, ...] = (),
    ) -> InventoryTelegramResult:
        return InventoryTelegramResult(
            state=state,
            screen=InventoryTelegramScreen(text, tuple(rows)),
            notice=notice,
            error_class=error_class,
            item_count=item_count,
            continuations=continuations,
        )

    @staticmethod
    def _callback(
        action: str,
        view: InventorySnapshotView | None = None,
        *,
        position: int | None = None,
        page: int = 0,
    ) -> str:
        parts = ["inventory", "v1", action]
        if view is not None:
            parts.extend((view.snapshot.id, str(view.snapshot.source_revision)))
        if position is not None:
            parts.append(str(position))
        if action == "r":
            parts.append(str(max(page, 0)))
        value = ":".join(parts)
        if len(value.encode("utf-8")) > INVENTORY_MAX_CALLBACK_BYTES:
            raise ValueError("callback too long")
        return value

    def _home(self, actor: object) -> InventoryTelegramResult:
        text_gate, photo_gate = self._gate("text", actor), self._gate("photo", actor)
        if not text_gate.ready and not photo_gate.ready:
            return self._result(
                "disabled", INVENTORY_PLACEHOLDER_REPLY, error_class="disabled"
            )
        rows = []
        if text_gate.ready:
            rows.append((("Ввести список текстом", self._callback("t")),))
        if photo_gate.ready:
            rows.append((("Отправить фотографию", self._callback("p")),))
        rows.extend((
            (("Последний подтверждённый список", self._callback("l")),),
            (("Отмена", self._callback("b")),),
        ))
        return self._result(
            "home",
            "<b>🥕 Продукты дома</b>\n\nДобавьте список или фото. Перед подтверждением всё можно проверить и исправить.",
            rows=rows,
        )

    @staticmethod
    def _item_text(item: object) -> str:
        unit = getattr(item, "unit", None)
        if (
            getattr(item, "quantity_value", None) is None
            or getattr(unit, "value", unit) == "unknown"
        ):
            amount = "количество не указано"
        else:
            amount = f"{escape(str(item.quantity_value))} {escape(str(unit.value))}"
        uncertain = " · нужно уточнить" if getattr(item, "uncertainty", None) else ""
        return f"{escape(str(item.display_name))} — {amount}{uncertain}"

    def _review(
        self, view: InventorySnapshotView, *, page: int = 0, state: str = "review"
    ) -> InventoryTelegramResult:
        pages = max(
            1, (len(view.items) + INVENTORY_PAGE_SIZE - 1) // INVENTORY_PAGE_SIZE
        )
        current = min(max(page, 0), pages - 1)
        visible = view.items[
            current * INVENTORY_PAGE_SIZE : (current + 1) * INVENTORY_PAGE_SIZE
        ]
        lines = [
            "<b>🥕 Проверьте список</b>",
            "Сначала проверьте продукты, затем подтвердите снимок.",
        ]
        lines.extend(f"{item.position}. {self._item_text(item)}" for item in visible)
        rows = [
            (
                (
                    "Изм. " + str(item.position),
                    self._callback("e", view, position=item.position),
                ),
                (
                    "Удал. " + str(item.position),
                    self._callback("d", view, position=item.position),
                ),
            )
            for item in visible
        ]
        navigation = []
        if current:
            navigation.append(("◀", self._callback("r", view, page=current - 1)))
        if current + 1 < pages:
            navigation.append(("▶", self._callback("r", view, page=current + 1)))
        if navigation:
            rows.append(tuple(navigation))
        rows.extend((
            (("➕ Добавить", self._callback("a", view)),),
            (
                ("✅ Подтвердить", self._callback("c", view)),
                ("Отменить", self._callback("x", view)),
            ),
            (("Начать заново", self._callback("h")),),
        ))
        return self._result(
            state, "\n".join(lines), rows=rows, item_count=len(view.items)
        )

    @staticmethod
    def _chunk_lines(lines: list[str]) -> tuple[str, ...]:
        chunks: list[str] = []
        current = ""
        for line in lines:
            addition = line if not current else "\n" + line
            if current and len(current) + len(addition) > WEEKLY_MENU_MAX_CHUNK_LENGTH:
                chunks.append(current)
                current = line
            else:
                current += addition
        if current:
            chunks.append(current)
        return tuple(chunks)

    def _confirmed(self, view: InventorySnapshotView) -> InventoryTelegramResult:
        lines = [
            "<b>Список подтверждён</b>",
            "Подтверждённые продукты:",
            *(f"{item.position}. {self._item_text(item)}" for item in view.items),
            "",
            "Можно создать только черновик меню. "
            "Публикация и список покупок не изменяются.",
        ]
        chunks = self._chunk_lines(lines)
        return self._result(
            "confirmed",
            chunks[0],
            continuations=chunks[1:],
            rows=(
                (("📅 Составить меню на неделю", self._callback("g", view)),),
                (("📄 Показать черновик", self._callback("v", view)),),
                (("🥕 Новый список", self._callback("h")),),
                (("Отмена", self._callback("b")),),
            ),
            item_count=len(view.items),
        )

    @staticmethod
    def _draft_chunks(view: WeeklyMenuRevisionView) -> tuple[str, ...]:
        blocks = []
        slots = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин"}
        by_date = {}
        for entry in view.entries:
            by_date.setdefault(entry.local_date, []).append(entry)
        slot_order = {"breakfast": 0, "lunch": 1, "dinner": 2}
        for local_date in sorted(by_date):
            lines = [f"<b>{escape(local_date)}</b>"]
            for entry in sorted(
                by_date[local_date],
                key=lambda item: (
                    slot_order.get(item.meal_slot.value, 99),
                    item.position,
                ),
            ):
                servings = (
                    f", порций: {escape(str(entry.servings))}" if entry.servings else ""
                )
                description = str(entry.description or "")
                macro = (
                    "\n" + escape(description.split("\n", 1)[0])
                    if description.startswith("КБЖУ на порцию:")
                    else ""
                )
                lines.append(
                    f"{slots.get(entry.meal_slot.value, entry.meal_slot.value)}: <b>{escape(str(entry.title))}</b>{servings}{macro}"
                )
            blocks.append("\n".join(lines))
        chunks, current = [], "<b>📋 Черновик меню</b>"
        for block in blocks:
            addition = "\n\n" + block
            if len(current) + len(addition) > WEEKLY_MENU_MAX_CHUNK_LENGTH:
                chunks.append(current)
                current = block
            else:
                current += addition
        if current:
            chunks.append(current)
        return tuple(chunks)

    def _draft(
        self, draft: WeeklyMenuRevisionView, snapshot: InventorySnapshotView
    ) -> InventoryTelegramResult:
        chunks = self._draft_chunks(draft)
        return self._result(
            "draft",
            chunks[0],
            continuations=chunks[1:],
            rows=(
                (("✅ Одобрить меню", self._callback("ap", snapshot)),),
                (("🔄 Перегенерировать", self._callback("rg", snapshot)),),
                (("🥕 Изменить продукты", self._callback("h")),),
                (("Отмена", self._callback("b")),),
            ),
            item_count=len(draft.entries),
        )

    def home(self, actor_user_id: object) -> InventoryTelegramResult:
        return self._home(actor_user_id)

    def pending_input_kind(self, actor_user_id: object) -> str | None:
        actor = _positive_actor(actor_user_id)
        return (
            None if actor is None else getattr(self._pending.get(actor), "mode", None)
        )

    def _load(
        self,
        actor_user_id: object,
        callback: _InventoryCallback,
        *,
        allowed_statuses: frozenset[str],
    ):
        if callback.snapshot_id is None or callback.source_revision is None:
            return None
        try:
            actor, scope, context = self._resolve_scope(actor_user_id)
            view = self._store().get_snapshot(scope, callback.snapshot_id)
        except (
            HouseholdAccessError,
            HouseholdIntegrityError,
            HouseholdNotFoundError,
            HouseholdValidationError,
            InventoryAccessError,
            InventoryNotFoundError,
            InventoryStateError,
            InventoryValidationError,
            OSError,
        ):
            return None
        if (
            view.snapshot.source_revision != callback.source_revision
            or view.snapshot.status.value not in allowed_statuses
        ):
            return None
        return actor, scope, context, view

    def handle_callback(
        self, actor_user_id: object, callback_data: object
    ) -> InventoryTelegramResult:
        callback = parse_inventory_callback(callback_data)
        if callback is None:
            return self._result(
                "stale", INVENTORY_UNAVAILABLE_REPLY, error_class="invalid_callback"
            )
        if callback.action == "h":
            actor = _positive_actor(actor_user_id)
            if actor is not None:
                self._pending.pop(actor, None)
            return self.home(actor_user_id)
        if callback.action == "b":
            return self._result("back", "Главное меню.")
        if callback.action in {"t", "p"}:
            kind = "text" if callback.action == "t" else "photo"
            if not self._gate(kind, actor_user_id).ready:
                return self._result(
                    "disabled", INVENTORY_PLACEHOLDER_REPLY, error_class="disabled"
                )
            try:
                actor, _scope, _context = self._resolve_scope(actor_user_id)
            except (
                HouseholdAccessError,
                HouseholdIntegrityError,
                HouseholdNotFoundError,
                HouseholdValidationError,
                OSError,
            ):
                return self._result(
                    "unavailable",
                    INVENTORY_UNAVAILABLE_REPLY,
                    error_class="household_unavailable",
                )
            self._pending[actor] = _PendingInput(kind)
            return self._result(
                "awaiting_" + kind,
                INVENTORY_TEXT_PROMPT if kind == "text" else INVENTORY_PHOTO_PROMPT,
            )
        if callback.action == "l":
            if (
                not self._gate("text", actor_user_id).ready
                and not self._gate("photo", actor_user_id).ready
            ):
                return self._result(
                    "disabled", INVENTORY_PLACEHOLDER_REPLY, error_class="disabled"
                )
            try:
                _actor, scope, _context = self._resolve_scope(actor_user_id)
                view = self._store().get_latest_confirmed_snapshot(scope)
            except (
                HouseholdAccessError,
                HouseholdIntegrityError,
                HouseholdNotFoundError,
                HouseholdValidationError,
                InventoryAccessError,
                OSError,
            ):
                return self._result(
                    "unavailable",
                    INVENTORY_UNAVAILABLE_REPLY,
                    error_class="unavailable",
                )
            if view is None:
                return self._result("empty", INVENTORY_EMPTY_REPLY)
            if not self._gate(view.snapshot.source_type.value, actor_user_id).ready:
                return self._result(
                    "disabled", INVENTORY_PLACEHOLDER_REPLY, error_class="disabled"
                )
            return self._confirmed(view)
        if callback.action not in {"r", "a", "e", "d", "c", "x", "g", "rg", "v", "ap"}:
            return self._result(
                "stale", INVENTORY_UNAVAILABLE_REPLY, error_class="invalid_callback"
            )
        pending_actions = {"r", "a", "e", "d", "x"}
        allowed_statuses = (
            frozenset({"pending"})
            if callback.action in pending_actions
            else frozenset({"pending", "confirmed"})
            if callback.action == "c"
            else frozenset({"confirmed"})
        )
        loaded = self._load(
            actor_user_id,
            callback,
            allowed_statuses=allowed_statuses,
        )
        if loaded is None:
            return self._result(
                "stale", INVENTORY_UNAVAILABLE_REPLY, error_class="stale"
            )
        actor, scope, context, view = loaded
        if not self._gate(view.snapshot.source_type.value, actor).ready:
            return self._result(
                "disabled", INVENTORY_PLACEHOLDER_REPLY, error_class="disabled"
            )
        if (
            callback.action in {"g", "rg", "v", "ap"}
            and not self._gate("weekly", actor).ready
        ):
            return self._result(
                "disabled", INVENTORY_PLACEHOLDER_REPLY, error_class="disabled"
            )
        if callback.action == "r":
            return self._review(view, page=callback.page)
        if callback.action == "a":
            self._pending[actor] = _PendingInput(
                "add", view.snapshot.id, view.snapshot.source_revision
            )
            return self._result("awaiting_add", INVENTORY_ADD_PROMPT)
        if callback.action == "e":
            if not any(item.position == callback.position for item in view.items):
                return self._result(
                    "stale", INVENTORY_UNAVAILABLE_REPLY, error_class="stale"
                )
            self._pending[actor] = _PendingInput(
                "edit",
                view.snapshot.id,
                view.snapshot.source_revision,
                callback.position,
            )
            return self._result("awaiting_edit", INVENTORY_EDIT_PROMPT)
        if callback.action == "d":
            retained = [
                item for item in view.items if item.position != callback.position
            ]
            if len(retained) == len(view.items) or not retained:
                return self._result(
                    "invalid_input",
                    INVENTORY_UNAVAILABLE_REPLY,
                    error_class="invalid_input",
                )
            inputs = [
                InventoryItemInput(
                    item.display_name,
                    item.quantity_value,
                    item.unit,
                    item.category,
                    item.confidence,
                    item.uncertainty,
                )
                for item in retained
            ]
            try:
                updated = self._store().replace_pending_items(
                    scope,
                    view.snapshot.id,
                    inputs,
                    expected_source_revision=callback.source_revision,
                )
            except (
                InventoryAccessError,
                InventoryStateError,
                InventoryValidationError,
                OSError,
            ):
                return self._result(
                    "unavailable",
                    INVENTORY_UNAVAILABLE_REPLY,
                    error_class="unavailable",
                )
            return self._review(updated, state="deleted")
        if callback.action == "c":
            try:
                confirmed = self._store().confirm_snapshot(
                    scope,
                    view.snapshot.id,
                    expected_source_revision=callback.source_revision,
                )
            except (InventoryAccessError, InventoryStateError, OSError):
                return self._result(
                    "unavailable",
                    INVENTORY_UNAVAILABLE_REPLY,
                    error_class="unavailable",
                )
            self._pending.pop(actor, None)
            return self._confirmed(confirmed)
        if callback.action == "x":
            try:
                self._store().cancel_snapshot(
                    scope,
                    view.snapshot.id,
                    expected_source_revision=callback.source_revision,
                )
            except (InventoryAccessError, InventoryStateError, OSError):
                return self._result(
                    "unavailable",
                    INVENTORY_UNAVAILABLE_REPLY,
                    error_class="unavailable",
                )
            self._pending.pop(actor, None)
            return self._result("cancelled", "Список отменён.")
        if callback.action == "v":
            draft = self._current_draft(context)
            return (
                self._draft(draft, view) if draft is not None else self._confirmed(view)
            )
        if callback.action == "ap":
            return self._result(
                "approval_unavailable", "Публикация меню пока недоступна."
            )
        return self._generate(actor, view)

    def handle_text(
        self, actor_user_id: object, text: str
    ) -> InventoryTelegramResult | None:
        actor = _positive_actor(actor_user_id)
        pending = None if actor is None else self._pending.get(actor)
        if pending is None:
            return None
        if pending.mode == "photo":
            return self._result("awaiting_photo", INVENTORY_PHOTO_PROMPT)
        if str(text).strip().lower() == "/cancel":
            self._pending.pop(actor, None)
            return self._result("cancelled", "Ввод списка отменён.")
        if str(text).lstrip().startswith("/"):
            return None
        if not self._gate("text", actor).ready:
            return self._result(
                "disabled", INVENTORY_PLACEHOLDER_REPLY, error_class="disabled"
            )
        try:
            _actor, scope, _context = self._resolve_scope(actor)
            parsed = parse_inventory_text(text)
            if len(parsed) > INVENTORY_MAX_ITEMS:
                raise InventoryValidationError("inventory item limit exceeded")
            store = self._store()
            if pending.mode == "text":
                view = store.create_text_snapshot(scope, text)
            else:
                if len(parsed) != 1 or pending.snapshot_id is None:
                    prompt = (
                        INVENTORY_EDIT_PROMPT
                        if pending.mode == "edit"
                        else INVENTORY_ADD_PROMPT
                    )
                    return self._result(
                        "invalid_input", prompt, error_class="invalid_input"
                    )
                existing = store.get_snapshot(scope, pending.snapshot_id)
                if (
                    existing.snapshot.status.value != "pending"
                    or existing.snapshot.source_revision != pending.source_revision
                ):
                    return self._result(
                        "stale", INVENTORY_UNAVAILABLE_REPLY, error_class="stale"
                    )
                inputs = [
                    InventoryItemInput(
                        item.display_name,
                        item.quantity_value,
                        item.unit,
                        item.category,
                        item.confidence,
                        item.uncertainty,
                    )
                    for item in existing.items
                ]
                if pending.mode == "add":
                    inputs.append(parsed[0])
                elif (
                    pending.mode == "edit"
                    and pending.position is not None
                    and 0 < pending.position <= len(inputs)
                ):
                    inputs[pending.position - 1] = parsed[0]
                else:
                    return self._result(
                        "stale", INVENTORY_UNAVAILABLE_REPLY, error_class="stale"
                    )
                view = store.replace_pending_items(
                    scope,
                    existing.snapshot.id,
                    inputs,
                    expected_source_revision=pending.source_revision,
                )
        except (InventoryValidationError, ValueError):
            prompt = (
                INVENTORY_TEXT_PROMPT
                if pending.mode == "text"
                else INVENTORY_EDIT_PROMPT
                if pending.mode == "edit"
                else INVENTORY_ADD_PROMPT
            )
            return self._result("invalid_input", prompt, error_class="invalid_input")
        except (
            HouseholdAccessError,
            HouseholdIntegrityError,
            HouseholdNotFoundError,
            HouseholdValidationError,
            InventoryAccessError,
            InventoryStateError,
            OSError,
        ):
            return self._result(
                "unavailable", INVENTORY_UNAVAILABLE_REPLY, error_class="unavailable"
            )
        self._pending.pop(actor, None)
        return self._review(view)

    async def handle_photo_bytes(
        self, actor_user_id: object, image_bytes: bytes
    ) -> InventoryTelegramResult | None:
        actor = _positive_actor(actor_user_id)
        pending = None if actor is None else self._pending.get(actor)
        if pending is None:
            return None
        if pending.mode != "photo":
            return self._result("awaiting_text", INVENTORY_TEXT_PROMPT)
        if not self._gate("photo", actor).ready:
            return self._result(
                "disabled", INVENTORY_PLACEHOLDER_REPLY, error_class="disabled"
            )
        if not image_bytes:
            self._pending[actor] = _PendingInput("text")
            return self._result(
                "vision_unavailable",
                INVENTORY_TEXT_PROMPT,
                error_class="vision_unavailable",
            )
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix="healbite-inventory-", suffix=".jpg", delete=False
            ) as handle:
                handle.write(bytes(image_bytes))
                path = Path(handle.name)
            raw = await self._vision_analyze_fn(
                str(path),
                "Return only JSON with an items list. Each item has name, quantity_value, unit, uncertain. Identify only visible food products.",
            )
            candidates = self._photo_candidates(raw)
            if not self._gate("photo", actor).ready:
                return self._result(
                    "disabled", INVENTORY_PLACEHOLDER_REPLY, error_class="disabled"
                )
            _actor, scope, _context = self._resolve_scope(actor)
            view = self._store().create_photo_candidate(scope, candidates)
        except Exception:
            self._pending[actor] = _PendingInput("text")
            return self._result(
                "vision_unavailable",
                INVENTORY_TEXT_PROMPT,
                error_class="vision_unavailable",
            )
        finally:
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self._pending.pop(actor, None)
        return self._review(view)

    @staticmethod
    def _photo_candidates(raw: object) -> tuple[InventoryItemInput, ...]:
        outer = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(outer, dict) or outer.get("success") is not True:
            raise ValueError("vision unavailable")
        analysis = outer.get("analysis")
        payload = json.loads(analysis) if isinstance(analysis, str) else analysis
        if (
            not isinstance(payload, dict)
            or set(payload) != {"items"}
            or not isinstance(payload["items"], list)
        ):
            raise ValueError("invalid candidate shape")
        items = []
        for candidate in payload["items"]:
            if not isinstance(candidate, dict) or set(candidate) - {
                "name",
                "quantity_value",
                "unit",
                "uncertain",
            }:
                raise ValueError("invalid candidate")
            if (
                not isinstance(candidate.get("name"), str)
                or not candidate["name"].strip()
            ):
                raise ValueError("invalid candidate")
            items.append(
                InventoryItemInput(
                    display_name=candidate["name"],
                    quantity_value=None
                    if candidate.get("quantity_value") is None
                    else str(candidate["quantity_value"]),
                    unit="unknown"
                    if candidate.get("unit") is None
                    else str(candidate["unit"]),
                    uncertainty="needs_confirmation"
                    if candidate.get("uncertain") is True
                    else None,
                )
            )
            if len(items) > INVENTORY_MAX_ITEMS:
                raise ValueError("candidate item limit exceeded")
        if not items:
            raise ValueError("empty candidate list")
        return tuple(items)

    def _current_draft(self, context: object) -> WeeklyMenuRevisionView | None:
        try:
            store = HealBiteWeeklyMenuStore(db_path=self._db_path)
            series = store.get_weekly_menu_series(
                context,
                context.household_id,
                current_week_start(now=self._now_factory()),
            )
            if series is None:
                return None
            revision = next(
                (
                    item
                    for item in reversed(
                        store.list_weekly_menu_revisions(context, series.id)
                    )
                    if item.status is WeeklyMenuRevisionStatus.DRAFT
                ),
                None,
            )
            return (
                None
                if revision is None
                else store.get_weekly_menu_revision(context, revision.id)
            )
        except Exception:
            return None

    def _generate(
        self, actor: int, snapshot: InventorySnapshotView
    ) -> InventoryTelegramResult:
        week = current_week_start(now=self._now_factory())
        key = (actor, snapshot.snapshot.id, week)
        with self._generation_attempts_lock:
            attempts = self._generation_attempts.get(key, 0)
            if attempts >= INVENTORY_MAX_GENERATION_ATTEMPTS:
                return self._result(
                    "generation_limited",
                    INVENTORY_UNAVAILABLE_REPLY,
                    error_class="generation_limited",
                )
            self._generation_attempts[key] = attempts + 1
        try:
            result = self._generation_service_factory().generate_draft_for_week(
                actor,
                week,
                idempotency_key=hashlib.sha256(
                    f"inventory-ui:{snapshot.snapshot.id}:{week}:{attempts + 1}".encode()
                ).hexdigest(),
                locale="ru-RU",
                max_entries=21,
                inventory_snapshot_id=snapshot.snapshot.id,
            )
        except Exception:
            return self._result(
                "generation_failed",
                INVENTORY_UNAVAILABLE_REPLY,
                error_class="generation_unavailable",
            )
        if (
            result.status is not WeeklyMenuGenerationStatus.SUCCESS
            or result.revision_view is None
        ):
            return self._result(
                "generation_failed",
                INVENTORY_UNAVAILABLE_REPLY,
                error_class="generation_" + result.status.value,
            )
        return self._draft(result.revision_view, snapshot)


def build_inventory_telegram_controller(
    *, env: dict[str, str] | None = None, db_path: str | Path | None = None
) -> HealBiteInventoryTelegramController:
    return HealBiteInventoryTelegramController(
        text_config=load_feature_gate_config("HEALBITE_INVENTORY_TEXT_UI", env=env),
        photo_config=load_feature_gate_config("HEALBITE_INVENTORY_PHOTO_UI", env=env),
        weekly_generation_config=load_feature_gate_config(
            "HEALBITE_INVENTORY_WEEKLY_GENERATION_UI", env=env
        ),
        db_path=db_path,
    )
