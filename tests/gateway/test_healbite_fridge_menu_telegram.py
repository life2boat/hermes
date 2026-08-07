from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_fridge_menu import (
    FridgeMenuContractError,
    FridgeMenuLLMGenerator,
    parse_fridge_menu_response,
)
from gateway.healbite_fridge_menu_schema import apply_fridge_menu_schema
from gateway.healbite_fridge_menu_telegram import (
    FRIDGE_MENU_COMMAND,
    FRIDGE_MENU_MAX_CHUNK_LENGTH,
    HealBiteFridgeMenuTelegramController,
    format_fridge_menu_plan,
)
from gateway.healbite_weekly_menu_runtime import build_fridge_weekly_menu_prompts
from gateway.platforms.telegram import (
    HEALBITE_REPLY_KEYBOARD_ACTIONS,
    TelegramAdapter,
)

ACTOR = 8_000_000_000_000_003_101
OTHER_ACTOR = 8_000_000_000_000_003_102
WEEK_START = "2026-08-03"


def _gate(*actors: int, enabled: bool = True) -> FeatureGateConfig:
    return FeatureGateConfig(enabled=enabled, allowlist=frozenset(actors))


def _payload(*, title_suffix: str = "", unsafe_title: bool = False) -> dict:
    days = []
    for day in (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    ):
        meals = []
        for meal_type in ("breakfast", "lunch", "dinner"):
            title = f"{meal_type} блюдо{title_suffix}"
            if unsafe_title and day == "monday" and meal_type == "breakfast":
                title = "Омлет <опасный> & вкусный"
            meals.append(
                {
                    "meal_type": meal_type,
                    "title": title,
                    "ingredients": [
                        {
                            "name": "Яйца",
                            "quantity": 2,
                            "unit": "piece",
                            "is_in_inventory": True,
                        },
                        {
                            "name": "Рис",
                            "quantity": 100,
                            "unit": "g",
                            "is_in_inventory": False,
                        },
                    ],
                }
            )
        days.append({"day": day, "meals": meals})
    return {
        "days": days,
        "missing_ingredients_to_buy": [
            {"name": "Рис", "quantity": 2100, "unit": "g"}
        ],
    }


def _plan(*, title_suffix: str = "", unsafe_title: bool = False):
    return parse_fridge_menu_response(
        _payload(title_suffix=title_suffix, unsafe_title=unsafe_title),
        inventory_ingredients=("Яйца", "Молоко"),
    )


class _Generator:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def generate(self, inventory_ingredients, **kwargs):
        inventory = tuple(inventory_ingredients)
        self.calls.append((inventory, dict(kwargs)))
        return _plan(title_suffix=f" {len(self.calls)}")


def _seed_db(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT)"
        )
        connection.executemany(
            "INSERT INTO users (user_id, username) VALUES (?, ?)",
            ((ACTOR, "synthetic"), (OTHER_ACTOR, "other")),
        )
        apply_fridge_menu_schema(connection)


def _controller(db_path, generator, *, vision_text_fn=None):
    return HealBiteFridgeMenuTelegramController(
        config=_gate(ACTOR),
        db_path=db_path,
        generator_factory=lambda: generator,
        vision_text_fn=vision_text_fn,
        now_factory=lambda: datetime(2026, 8, 7, tzinfo=timezone.utc),
    )


def _find_callback(result, label_fragment: str) -> str:
    for row in result.screen.rows:
        for label, callback_data in row:
            if label_fragment in label:
                return callback_data
    raise AssertionError(f"callback not found: {label_fragment}")


def _message(*, text=None, photo=None):
    return SimpleNamespace(
        text=text,
        message_id=77,
        from_user=SimpleNamespace(
            id=ACTOR,
            username="synthetic",
            first_name="Synthetic",
            full_name="Synthetic",
        ),
        chat=SimpleNamespace(id=555, type="private", title=None, full_name=None),
        chat_id=555,
        message_thread_id=None,
        reply_to_message=None,
        photo=photo or [],
        document=None,
        video=None,
        audio=None,
        voice=None,
        sticker=None,
        caption=None,
        media_group_id=None,
        date=datetime(2026, 8, 7, tzinfo=timezone.utc),
    )


def _adapter(controller):
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="synthetic-token", extra={})
    )
    adapter._fridge_menu_telegram = controller
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True
    adapter._maybe_handle_healbite_onboarding_reply = AsyncMock(return_value=False)
    adapter._maybe_handle_healbite_inventory_pending_text = AsyncMock(
        return_value=False
    )
    adapter._maybe_handle_healbite_weight_pending_reply = AsyncMock(
        return_value=False
    )
    adapter._maybe_handle_healbite_water_pending_reply = AsyncMock(
        return_value=False
    )
    adapter._ensure_forum_commands = AsyncMock()
    adapter._enqueue_text_event = Mock()
    adapter.handle_message = AsyncMock()
    return adapter


def test_strict_response_contract_requires_order_and_exact_shopping_relation():
    plan = _plan()

    assert len(plan.days) == 7
    assert all(len(day.meals) == 3 for day in plan.days)
    assert [item.name for item in plan.missing_ingredients_to_buy] == ["Рис"]

    wrong_order = _payload()
    wrong_order["days"][0], wrong_order["days"][1] = (
        wrong_order["days"][1],
        wrong_order["days"][0],
    )
    with pytest.raises(FridgeMenuContractError, match="ordered"):
        parse_fridge_menu_response(
            wrong_order,
            inventory_ingredients=("Яйца", "Молоко"),
        )

    missing_mismatch = _payload()
    missing_mismatch["missing_ingredients_to_buy"] = []
    with pytest.raises(FridgeMenuContractError, match="shopping list"):
        parse_fridge_menu_response(
            missing_mismatch,
            inventory_ingredients=("Яйца", "Молоко"),
        )


def test_contract_rejects_inventory_flag_that_contradicts_user_input():
    payload = _payload()
    payload["days"][0]["meals"][0]["ingredients"][0][
        "is_in_inventory"
    ] = False

    with pytest.raises(FridgeMenuContractError, match="contradicts"):
        parse_fridge_menu_response(
            payload,
            inventory_ingredients=("Яйца", "Молоко"),
        )


def test_llm_generator_uses_stage_one_prompt_builder_and_one_request_policy():
    calls = []

    def call_llm_fn(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(_payload(), ensure_ascii=False)
                    )
                )
            ]
        )

    generator = FridgeMenuLLMGenerator(call_llm_fn=call_llm_fn)
    plan = generator.generate(("Яйца", "Молоко"), week_start=WEEK_START)
    expected = build_fridge_weekly_menu_prompts(
        ("Яйца", "Молоко"),
        week_start=WEEK_START,
    )

    assert len(plan.days) == 7
    assert len(calls) == 1
    assert calls[0]["messages"] == [
        {"role": "system", "content": expected.system_prompt},
        {"role": "user", "content": expected.user_prompt},
    ]
    assert calls[0]["call_policy"].max_external_requests == 1


def test_formatter_uses_html_labels_escapes_titles_and_separates_shopping():
    chunks = format_fridge_menu_plan(
        _plan(unsafe_title=True),
        week_start=WEEK_START,
    )
    rendered = "\n".join(chunks)

    assert all(len(chunk) <= FRIDGE_MENU_MAX_CHUNK_LENGTH for chunk in chunks)
    assert "<b>Понедельник, 03.08</b>" in rendered
    assert "Завтрак:" in rendered
    assert "breakfast:" not in rendered
    assert "Омлет &lt;опасный&gt; &amp; вкусный" in rendered
    assert "<b>📝 Список покупок</b>" in rendered
    assert "• Рис — 2100 г" in rendered


def test_text_fsm_generates_regenerates_and_saves_all_tables(tmp_path):
    db_path = tmp_path / "fridge-menu.db"
    _seed_db(db_path)
    generator = _Generator()
    controller = _controller(db_path, generator)

    home = controller.home(ACTOR)
    preview = controller.handle_text(ACTOR, "Яйца, молоко\nяйца")

    assert home.state == "awaiting_input"
    assert controller.pending_input_kind(ACTOR) is None
    assert preview is not None and preview.state == "preview"
    assert preview.item_count == 21
    assert generator.calls[0][0] == ("Яйца", "молоко")
    assert generator.calls[0][1]["week_start"] == WEEK_START
    assert _find_callback(preview, "Сохранить меню").startswith("fridge:v1:s:")
    assert _find_callback(preview, "Сгенерировать заново").startswith(
        "fridge:v1:r:"
    )
    assert _find_callback(preview, "Отмена").startswith("fridge:v1:x:")

    regenerated = controller.handle_callback(
        ACTOR,
        _find_callback(preview, "Сгенерировать заново"),
    )
    assert regenerated.state == "preview"
    assert len(generator.calls) == 2
    saved = controller.handle_callback(
        ACTOR,
        _find_callback(regenerated, "Сохранить меню"),
    )

    assert saved.state == "saved"
    assert controller.pending_input_kind(ACTOR) is None
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_inventory").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM weekly_menu_plans").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM planned_meals").fetchone()[0] == 21
        assert connection.execute("SELECT COUNT(*) FROM planned_ingredients").fetchone()[0] == 42
        assert connection.execute(
            "SELECT COUNT(*) FROM planned_ingredients WHERE is_in_inventory = 0"
        ).fetchone()[0] == 21

    replacement = controller.home(ACTOR)
    assert replacement.state == "awaiting_input"
    replacement_preview = controller.handle_text(ACTOR, "Яйца")
    assert replacement_preview is not None
    replacement_saved = controller.handle_callback(
        ACTOR,
        _find_callback(replacement_preview, "Сохранить меню"),
    )
    assert replacement_saved.state == "saved"

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM user_inventory").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM weekly_menu_plans").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM planned_meals").fetchone()[0] == 21
        assert connection.execute("SELECT COUNT(*) FROM planned_ingredients").fetchone()[0] == 42


def test_invalid_text_cancel_and_cross_user_callbacks_fail_closed(tmp_path):
    db_path = tmp_path / "fsm.db"
    _seed_db(db_path)
    generator = _Generator()
    controller = _controller(db_path, generator)

    controller.home(ACTOR)
    invalid = controller.handle_text(ACTOR, "   ")
    assert invalid is not None and invalid.state == "invalid_input"
    assert controller.pending_input_kind(ACTOR) == "text_or_photo"
    assert generator.calls == []

    preview = controller.handle_text(ACTOR, "Яйца, молоко")
    assert preview is not None
    foreign = controller.handle_callback(
        OTHER_ACTOR,
        _find_callback(preview, "Сохранить меню"),
    )
    assert foreign.state == "stale"
    cancelled = controller.handle_callback(
        ACTOR,
        _find_callback(preview, "Отмена"),
    )
    assert cancelled.state == "cancelled"


@pytest.mark.asyncio
async def test_photo_fsm_uses_vision_text_stub_before_generation(tmp_path):
    db_path = tmp_path / "photo.db"
    _seed_db(db_path)
    generator = _Generator()
    vision = AsyncMock(return_value="- Яйца\n2. Молоко; яйца")
    controller = _controller(db_path, generator, vision_text_fn=vision)

    controller.home(ACTOR)
    result = await controller.handle_photo_bytes(ACTOR, b"synthetic-image")

    assert result is not None and result.state == "preview"
    vision.assert_awaited_once_with(b"synthetic-image")
    assert generator.calls[0][0] == ("Яйца", "Молоко")


def test_disabled_gate_does_not_start_fsm(tmp_path):
    controller = HealBiteFridgeMenuTelegramController(
        config=_gate(enabled=False),
        db_path=tmp_path / "disabled.db",
        generator_factory=_Generator,
    )

    result = controller.home(ACTOR)

    assert result.state == "disabled"
    assert controller.pending_input_kind(ACTOR) is None
    assert not (tmp_path / "disabled.db").exists()
    assert (
        HEALBITE_REPLY_KEYBOARD_ACTIONS["🥘 Из холодильника в меню"]
        == FRIDGE_MENU_COMMAND
    )


@pytest.mark.asyncio
async def test_adapter_consumes_fridge_text_locally_without_generic_dispatch(tmp_path):
    db_path = tmp_path / "adapter.db"
    _seed_db(db_path)
    generator = _Generator()
    controller = _controller(db_path, generator)
    adapter = _adapter(controller)
    controller.home(ACTOR)
    msg = _message(text="Яйца, молоко")
    update = SimpleNamespace(
        update_id=1,
        message=msg,
        effective_message=msg,
    )

    await adapter._handle_text_message(update, SimpleNamespace())

    adapter.handle_message.assert_not_awaited()
    assert adapter._send_message_with_thread_fallback.await_count >= 2
    sent_texts = [
        call.kwargs["text"]
        for call in adapter._send_message_with_thread_fallback.await_args_list
    ]
    assert sent_texts[0] == "Составляю меню…"
    assert any("📝 Список покупок" in text for text in sent_texts)


@pytest.mark.asyncio
async def test_adapter_consumes_fridge_photo_without_general_photo_cache(tmp_path):
    db_path = tmp_path / "photo-adapter.db"
    _seed_db(db_path)
    generator = _Generator()
    vision = AsyncMock(return_value="Яйца\nМолоко")
    controller = _controller(db_path, generator, vision_text_fn=vision)
    adapter = _adapter(controller)
    adapter._cache_photo_message_to_event = AsyncMock(return_value=False)
    controller.home(ACTOR)
    file_obj = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"photo"))
    )
    photo = SimpleNamespace(
        width=1024,
        height=768,
        file_size=5,
        get_file=AsyncMock(return_value=file_obj),
    )
    msg = _message(photo=[photo])
    update = SimpleNamespace(update_id=2, message=msg, effective_message=msg)

    await adapter._handle_media_message(update, SimpleNamespace())

    adapter.handle_message.assert_not_awaited()
    adapter._cache_photo_message_to_event.assert_not_awaited()
    vision.assert_awaited_once_with(b"photo")
    assert any(
        "Распознаю продукты" in call.kwargs["text"]
        for call in adapter._send_message_with_thread_fallback.await_args_list
    )
