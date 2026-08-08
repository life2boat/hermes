from __future__ import annotations

import asyncio
import base64
import secrets
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Awaitable, Callable, Protocol, Sequence

from agent.auxiliary_client import (
    VISION_SINGLE_REQUEST_LLM_CALL_POLICY,
    extract_content_or_reasoning,
    safe_async_call_llm,
)
from gateway.healbite_feature_gates import (
    FeatureGateConfig,
    evaluate_feature_gate,
    load_feature_gate_config,
)
from gateway.healbite_fridge_menu import (
    FridgeMenuContractError,
    FridgeMenuGenerationUnavailableError,
    FridgeMenuLLMGenerator,
    FridgeMenuPlan,
    FridgeMenuStorageError,
    FridgeMenuStore,
)
from gateway.healbite_weekly_menu_runtime import (
    WeeklyMenuPromptValidationError,
    parse_fridge_vision_ingredients,
)
from gateway.healbite_weekly_menu_telegram import current_week_start

FRIDGE_MENU_COMMAND = "/fridge_menu"
FRIDGE_MENU_CALLBACK_ROOT = "fridge:"
FRIDGE_MENU_CALLBACK_PREFIX = "fridge:v1:"
FRIDGE_MENU_PARSE_MODE = "HTML"
FRIDGE_MENU_MAX_CHUNK_LENGTH = 3500
FRIDGE_MENU_MAX_GENERATION_ATTEMPTS = 3
FRIDGE_MENU_MAX_IMAGE_BYTES = 10 * 1024 * 1024
FRIDGE_MENU_INPUT_PROMPT = (
    "<b>🥘 Из холодильника в меню</b>\n\n"
    "Отправьте список продуктов текстом — через запятую или с новой строки — "
    "либо одно фото холодильника.\n\n/cancel — отменить."
)
FRIDGE_MENU_UNAVAILABLE_REPLY = "Функция временно недоступна. Попробуйте позже."

_WEEKDAY_LABELS = {
    "monday": "Понедельник",
    "tuesday": "Вторник",
    "wednesday": "Среда",
    "thursday": "Четверг",
    "friday": "Пятница",
    "saturday": "Суббота",
    "sunday": "Воскресенье",
}
_MEAL_LABELS = {
    "breakfast": "Завтрак",
    "lunch": "Обед",
    "dinner": "Ужин",
}
_UNIT_LABELS = {
    "g": "г",
    "kg": "кг",
    "ml": "мл",
    "l": "л",
    "piece": "шт.",
    "package": "уп.",
    "unitless": "ед.",
    "unknown": "ед.",
}


class FridgeMenuGenerator(Protocol):
    def generate(
        self,
        inventory_ingredients: Sequence[str],
        *,
        week_start: str,
        dietary_restrictions: Sequence[str] = (),
        locale: str = "ru-RU",
    ) -> FridgeMenuPlan:
        ...


VisionTextFn = Callable[[bytes], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class FridgeMenuTelegramScreen:
    chunks: tuple[str, ...]
    rows: tuple[tuple[tuple[str, str], ...], ...] = ()
    parse_mode: str | None = FRIDGE_MENU_PARSE_MODE

    @property
    def text(self) -> str:
        return self.chunks[0]


@dataclass(frozen=True, slots=True)
class FridgeMenuTelegramResult:
    state: str
    screen: FridgeMenuTelegramScreen
    notice: str | None = None
    error_class: str | None = None
    item_count: int = 0


@dataclass(frozen=True, slots=True)
class _FridgeMenuSession:
    stage: str
    token: str
    inventory: tuple[str, ...] = ()
    source_type: str = "text"
    week_start: str | None = None
    plan: FridgeMenuPlan | None = None
    generation_attempts: int = 0


def _chunk_blocks(blocks: Sequence[str]) -> tuple[str, ...]:
    chunks: list[str] = []
    current = ""
    for block in blocks:
        parts: list[str] = []
        part = ""
        for line in block.splitlines() or [block]:
            addition = line if not part else "\n" + line
            if part and len(part) + len(addition) > FRIDGE_MENU_MAX_CHUNK_LENGTH:
                parts.append(part)
                part = line
            else:
                part += addition
        if part:
            parts.append(part)
        for block_part in parts:
            addition = block_part if not current else "\n\n" + block_part
            if current and len(current) + len(addition) > FRIDGE_MENU_MAX_CHUNK_LENGTH:
                chunks.append(current)
                current = block_part
            else:
                current += addition
    if current:
        chunks.append(current)
    return tuple(chunks or ("",))


def format_fridge_menu_plan(
    plan: FridgeMenuPlan,
    *,
    week_start: str,
) -> tuple[str, ...]:
    """Render a validated plan as bounded Telegram HTML chunks."""

    start = date.fromisoformat(week_start)
    end = start + timedelta(days=6)
    blocks = [
        "<b>🥘 Меню из продуктов дома</b>\n"
        f"Неделя {start.strftime('%d.%m')}–{end.strftime('%d.%m.%Y')}"
    ]
    for offset, day in enumerate(plan.days):
        local_date = start + timedelta(days=offset)
        lines = [
            f"<b>{_WEEKDAY_LABELS[day.day]}, {local_date.strftime('%d.%m')}</b>"
        ]
        lines.extend(
            f"{_MEAL_LABELS[meal.meal_type]}: {escape(meal.title)}"
            for meal in day.meals
        )
        blocks.append("\n".join(lines))

    shopping_lines = ["<b>📝 Список покупок</b>"]
    if not plan.missing_ingredients_to_buy:
        shopping_lines.append("Ничего докупать не нужно.")
    else:
        shopping_lines.extend(
            f"• {escape(item.name)} — {escape(item.quantity)} "
            f"{escape(_UNIT_LABELS[item.unit])}"
            for item in plan.missing_ingredients_to_buy
        )
    blocks.append("\n".join(shopping_lines))
    return _chunk_blocks(blocks)


class HealBiteFridgeMenuTelegramController:
    def __init__(
        self,
        *,
        config: FeatureGateConfig | None = None,
        db_path: str | Path | None = None,
        generator_factory: Callable[[], FridgeMenuGenerator] | None = None,
        store_factory: Callable[[], FridgeMenuStore] | None = None,
        vision_text_fn: VisionTextFn | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config or load_feature_gate_config(
            "HEALBITE_INVENTORY_WEEKLY_GENERATION_UI"
        )
        self._db_path = db_path
        self._generator_factory = generator_factory or FridgeMenuLLMGenerator
        self._store_factory = store_factory or (
            lambda: FridgeMenuStore(db_path=self._db_path)
        )
        self._vision_text_fn = vision_text_fn or self._default_vision_text
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._sessions: dict[int, _FridgeMenuSession] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _actor(value: object) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            actor = int(value)
        except (TypeError, ValueError):
            return None
        return actor if actor > 0 else None

    def _ready(self, actor: object) -> bool:
        return evaluate_feature_gate(self._config, actor).ready

    @staticmethod
    def _result(
        state: str,
        chunks: str | Sequence[str],
        *,
        rows: Sequence[Sequence[tuple[str, str]]] = (),
        notice: str | None = None,
        error_class: str | None = None,
        item_count: int = 0,
        parse_mode: str | None = FRIDGE_MENU_PARSE_MODE,
    ) -> FridgeMenuTelegramResult:
        normalized_chunks = (chunks,) if isinstance(chunks, str) else tuple(chunks)
        return FridgeMenuTelegramResult(
            state=state,
            screen=FridgeMenuTelegramScreen(
                chunks=normalized_chunks,
                rows=tuple(tuple(row) for row in rows),
                parse_mode=parse_mode,
            ),
            notice=notice,
            error_class=error_class,
            item_count=item_count,
        )

    @staticmethod
    def _callback(action: str, token: str | None = None) -> str:
        value = f"{FRIDGE_MENU_CALLBACK_PREFIX}{action}"
        if token is not None:
            value += f":{token}"
        if len(value.encode("utf-8")) > 64:
            raise ValueError("callback too long")
        return value

    async def _default_vision_text(self, image_bytes: bytes) -> str:
        data_url = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode(
            "ascii"
        )
        response = await safe_async_call_llm(
            task="vision",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Перечисли только видимые продукты, по одному на строку. "
                                "Не добавляй количества, markdown или пояснения."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            temperature=0,
            max_tokens=1000,
            call_policy=VISION_SINGLE_REQUEST_LLM_CALL_POLICY,
        )
        content = extract_content_or_reasoning(response)
        if not content:
            raise RuntimeError("vision response unavailable")
        return content

    def home(self, actor_user_id: object) -> FridgeMenuTelegramResult:
        actor = self._actor(actor_user_id)
        if actor is None or not self._ready(actor):
            return self._result(
                "disabled",
                FRIDGE_MENU_UNAVAILABLE_REPLY,
                error_class="disabled",
                parse_mode=None,
            )
        token = secrets.token_hex(8)
        with self._lock:
            self._sessions[actor] = _FridgeMenuSession(
                stage="awaiting_input", token=token
            )
        return self._result(
            "awaiting_input",
            FRIDGE_MENU_INPUT_PROMPT,
            rows=((('Отмена', self._callback('x', token)),),),
        )

    def pending_input_kind(self, actor_user_id: object) -> str | None:
        actor = self._actor(actor_user_id)
        if actor is None:
            return None
        with self._lock:
            session = self._sessions.get(actor)
            return "text_or_photo" if session and session.stage == "awaiting_input" else None

    def cancel_pending(self, actor_user_id: object) -> bool:
        actor = self._actor(actor_user_id)
        if actor is None:
            return False
        with self._lock:
            return self._sessions.pop(actor, None) is not None

    def _preview(
        self,
        *,
        plan: FridgeMenuPlan,
        week_start: str,
        token: str,
    ) -> FridgeMenuTelegramResult:
        return self._result(
            "preview",
            format_fridge_menu_plan(plan, week_start=week_start),
            rows=(
                (("💾 Сохранить меню", self._callback("s", token)),),
                (
                    ("🔄 Сгенерировать заново", self._callback("r", token)),
                    ("Отмена", self._callback("x", token)),
                ),
            ),
            item_count=sum(len(day.meals) for day in plan.days),
        )

    def _generate(
        self,
        actor: int,
        *,
        inventory: tuple[str, ...],
        source_type: str,
        expected_token: str | None,
    ) -> FridgeMenuTelegramResult:
        if not self._ready(actor):
            return self._result(
                "disabled",
                FRIDGE_MENU_UNAVAILABLE_REPLY,
                error_class="disabled",
                parse_mode=None,
            )
        with self._lock:
            session = self._sessions.get(actor)
            if session is None or session.stage not in {"awaiting_input", "preview"}:
                return self._result(
                    "stale",
                    FRIDGE_MENU_UNAVAILABLE_REPLY,
                    error_class="stale",
                    parse_mode=None,
                )
            if session.stage == "preview" and session.token != expected_token:
                return self._result(
                    "stale",
                    FRIDGE_MENU_UNAVAILABLE_REPLY,
                    error_class="stale",
                    parse_mode=None,
                )
            if session.generation_attempts >= FRIDGE_MENU_MAX_GENERATION_ATTEMPTS:
                return self._result(
                    "generation_limited",
                    "Лимит генераций для этого запуска исчерпан. Начните заново.",
                    error_class="generation_limited",
                    parse_mode=None,
                )
            week_start = session.week_start or current_week_start(now=self._now_factory())
            generating = replace(
                session,
                stage="generating",
                inventory=inventory,
                source_type=source_type,
                week_start=week_start,
                generation_attempts=session.generation_attempts + 1,
            )
            self._sessions[actor] = generating
        try:
            plan = self._generator_factory().generate(
                inventory,
                week_start=week_start,
                locale="ru-RU",
            )
        except (
            FridgeMenuContractError,
            FridgeMenuGenerationUnavailableError,
            WeeklyMenuPromptValidationError,
            OSError,
        ):
            with self._lock:
                current = self._sessions.get(actor)
                if current is not None and current.stage == "generating":
                    self._sessions[actor] = replace(current, stage="awaiting_input")
            return self._result(
                "generation_failed",
                FRIDGE_MENU_UNAVAILABLE_REPLY,
                rows=((('Отмена', self._callback('x', generating.token)),),),
                error_class="generation_failed",
                parse_mode=None,
            )
        token = secrets.token_hex(8)
        with self._lock:
            current = self._sessions.get(actor)
            if current is None or current.stage != "generating":
                return self._result(
                    "stale",
                    FRIDGE_MENU_UNAVAILABLE_REPLY,
                    error_class="stale",
                    parse_mode=None,
                )
            self._sessions[actor] = replace(
                current,
                stage="preview",
                plan=plan,
                token=token,
            )
        return self._preview(plan=plan, week_start=week_start, token=token)

    def handle_text(
        self,
        actor_user_id: object,
        text: str,
    ) -> FridgeMenuTelegramResult | None:
        actor = self._actor(actor_user_id)
        if actor is None:
            return None
        with self._lock:
            session = self._sessions.get(actor)
        if session is None or session.stage != "awaiting_input":
            return None
        normalized_text = str(text or "").strip()
        if normalized_text.casefold() == "/cancel":
            self.cancel_pending(actor)
            return self._result(
                "cancelled",
                "Создание меню отменено.",
                parse_mode=None,
            )
        if normalized_text.startswith("/"):
            return None
        try:
            inventory = tuple(parse_fridge_vision_ingredients(normalized_text))
        except WeeklyMenuPromptValidationError:
            inventory = ()
        if not inventory:
            return self._result(
                "invalid_input",
                FRIDGE_MENU_INPUT_PROMPT,
                rows=((('Отмена', self._callback('x', session.token)),),),
                error_class="invalid_input",
            )
        return self._generate(
            actor,
            inventory=inventory,
            source_type="text",
            expected_token=None,
        )

    async def handle_photo_bytes(
        self,
        actor_user_id: object,
        image_bytes: bytes,
    ) -> FridgeMenuTelegramResult | None:
        actor = self._actor(actor_user_id)
        if actor is None:
            return None
        with self._lock:
            session = self._sessions.get(actor)
        if session is None or session.stage != "awaiting_input":
            return None
        if not image_bytes or len(image_bytes) > FRIDGE_MENU_MAX_IMAGE_BYTES:
            return self._result(
                "vision_failed",
                "Не удалось прочитать фото. Отправьте другое фото или список текстом.",
                rows=((('Отмена', self._callback('x', session.token)),),),
                error_class="vision_failed",
                parse_mode=None,
            )
        try:
            vision_text = await self._vision_text_fn(bytes(image_bytes))
            inventory = tuple(parse_fridge_vision_ingredients(vision_text))
            if not inventory:
                raise WeeklyMenuPromptValidationError("empty vision ingredients")
        except Exception:
            return self._result(
                "vision_failed",
                "Не удалось распознать продукты. Отправьте другое фото или список текстом.",
                rows=((('Отмена', self._callback('x', session.token)),),),
                error_class="vision_failed",
                parse_mode=None,
            )
        return await asyncio.to_thread(
            self._generate,
            actor,
            inventory=inventory,
            source_type="vision",
            expected_token=None,
        )

    def handle_callback(
        self,
        actor_user_id: object,
        data: object,
    ) -> FridgeMenuTelegramResult:
        actor = self._actor(actor_user_id)
        text = str(data or "")
        parts = text.split(":")
        if actor is None or parts[:2] != ["fridge", "v1"] or len(parts) not in {3, 4}:
            return self._result(
                "stale",
                FRIDGE_MENU_UNAVAILABLE_REPLY,
                error_class="stale",
                parse_mode=None,
            )
        action = parts[2]
        token = parts[3] if len(parts) == 4 else None
        if action == "x":
            with self._lock:
                session = self._sessions.get(actor)
                if session is None or not token or token != session.token:
                    return self._result(
                        "stale",
                        FRIDGE_MENU_UNAVAILABLE_REPLY,
                        error_class="stale",
                        parse_mode=None,
                    )
                self._sessions.pop(actor, None)
            return self._result(
                "cancelled",
                "Создание меню отменено.",
                parse_mode=None,
            )
        with self._lock:
            session = self._sessions.get(actor)
        if (
            session is None
            or session.stage != "preview"
            or not token
            or token != session.token
            or session.plan is None
            or session.week_start is None
        ):
            return self._result(
                "stale",
                FRIDGE_MENU_UNAVAILABLE_REPLY,
                error_class="stale",
                parse_mode=None,
            )
        if action == "r":
            return self._generate(
                actor,
                inventory=session.inventory,
                source_type=session.source_type,
                expected_token=token,
            )
        if action != "s" or not self._ready(actor):
            return self._result(
                "stale",
                FRIDGE_MENU_UNAVAILABLE_REPLY,
                error_class="stale",
                parse_mode=None,
            )
        with self._lock:
            current = self._sessions.get(actor)
            if current != session:
                return self._result(
                    "stale",
                    FRIDGE_MENU_UNAVAILABLE_REPLY,
                    error_class="stale",
                    parse_mode=None,
                )
            self._sessions[actor] = replace(session, stage="saving")
        try:
            self._store_factory().save(
                user_id=actor,
                inventory_ingredients=session.inventory,
                source_type=session.source_type,
                week_start=session.week_start,
                plan=session.plan,
            )
        except FridgeMenuStorageError:
            with self._lock:
                current = self._sessions.get(actor)
                if current is not None and current.stage == "saving":
                    self._sessions[actor] = session
            return self._result(
                "save_failed",
                FRIDGE_MENU_UNAVAILABLE_REPLY,
                rows=(
                    (("💾 Сохранить меню", self._callback("s", token)),),
                    (("Отмена", self._callback("x", token)),),
                ),
                error_class="save_failed",
                parse_mode=None,
            )
        with self._lock:
            self._sessions.pop(actor, None)
        return self._result(
            "saved",
            "✅ Меню сохранено.",
            notice="Меню сохранено",
            item_count=sum(len(day.meals) for day in session.plan.days),
            parse_mode=None,
        )


def build_fridge_menu_telegram_controller(
    *,
    env: dict[str, str] | None = None,
    db_path: str | Path | None = None,
) -> HealBiteFridgeMenuTelegramController:
    return HealBiteFridgeMenuTelegramController(
        config=load_feature_gate_config(
            "HEALBITE_INVENTORY_WEEKLY_GENERATION_UI",
            env=env,
        ),
        db_path=db_path,
    )
