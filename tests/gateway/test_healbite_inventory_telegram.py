from __future__ import annotations

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_inventory import (
    HealBiteInventoryStore,
    InventoryOwnerScope,
    InventoryStatus,
)
from gateway.healbite_inventory_telegram import (
    INVENTORY_COMMAND,
    INVENTORY_MAX_GENERATION_ATTEMPTS,
    INVENTORY_PLACEHOLDER_REPLY,
    HealBiteInventoryTelegramController,
    InventoryTelegramResult,
    build_inventory_telegram_controller,
    parse_inventory_callback,
)
from gateway.healbite_weekly_menu_generation import (
    WeeklyMenuGenerationResult,
    WeeklyMenuGenerationStatus,
)
from gateway.healbite_weekly_menu_telegram import WEEKLY_MENU_MAX_CHUNK_LENGTH
from gateway.platforms.telegram import (
    HEALBITE_REPLY_KEYBOARD_ACTIONS,
    TelegramAdapter,
)


ACTOR = 8_000_000_000_000_002_101
OTHER_ACTOR = 8_000_000_000_000_002_102
WEEK_START = "2026-07-06"


def _gate(*actors: int, enabled: bool = True) -> FeatureGateConfig:
    return FeatureGateConfig(
        enabled=enabled,
        allowlist=frozenset(actors),
    )


def _seed_household(db_path: Path, actor: int = ACTOR):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(user_id INTEGER PRIMARY KEY, username TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO users "
            "(user_id, username, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (actor, "synthetic"),
        )
    return HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(
        actor
    )


def _controller(
    db_path: Path,
    *,
    text_enabled: bool = True,
    photo_enabled: bool = True,
    weekly_enabled: bool = True,
    vision_analyze_fn=None,
    generation_service=None,
) -> HealBiteInventoryTelegramController:
    actors = (ACTOR, OTHER_ACTOR)
    return HealBiteInventoryTelegramController(
        text_config=_gate(*actors, enabled=text_enabled),
        photo_config=_gate(*actors, enabled=photo_enabled),
        weekly_generation_config=_gate(*actors, enabled=weekly_enabled),
        db_path=db_path,
        vision_analyze_fn=vision_analyze_fn,
        generation_service_factory=(
            None if generation_service is None else lambda: generation_service
        ),
        now_factory=lambda: datetime(2026, 7, 8, tzinfo=timezone.utc),
    )


def _find_callback(
    result: InventoryTelegramResult,
    label_fragment: str,
) -> str:
    for row in result.screen.rows:
        for label, callback_data in row:
            if label_fragment in label:
                return callback_data
    raise AssertionError(f"callback not found: {label_fragment}")


def _snapshot_id(result: InventoryTelegramResult) -> str:
    parsed = parse_inventory_callback(_find_callback(result, "Подтвердить"))
    assert parsed is not None and parsed.snapshot_id is not None
    return parsed.snapshot_id


def _message(
    *,
    text: str | None = None,
    actor: int = ACTOR,
    message_id: int = 77,
    photo: list[object] | None = None,
):
    return SimpleNamespace(
        text=text,
        message_id=message_id,
        from_user=SimpleNamespace(
            id=actor,
            username="ignored",
            first_name="Ignored",
        ),
        chat=SimpleNamespace(id=555, type="private"),
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
    )


def _adapter(
    controller: HealBiteInventoryTelegramController,
) -> TelegramAdapter:
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="synthetic-token", extra={})
    )
    adapter._inventory_telegram = controller
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._enqueue_text_event = Mock()
    adapter._cache_photo_message_to_event = AsyncMock(return_value=False)
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True
    adapter._ensure_forum_commands = AsyncMock()
    return adapter


class _GenerationService:
    def __init__(self, *, long_titles: bool = False) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
        self.long_titles = long_titles

    def generate_draft_for_week(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        entries = []
        for day_offset in range(7):
            local_date = f"2026-07-{6 + day_offset:02d}"
            for position, slot in enumerate(
                ("breakfast", "lunch", "dinner"),
                start=1,
            ):
                suffix = " блюдо" * 160 if self.long_titles else " блюдо"
                entries.append(
                    SimpleNamespace(
                        local_date=local_date,
                        meal_slot=SimpleNamespace(value=slot),
                        position=position,
                        title=f"{slot}{suffix}",
                        description=(
                            "КБЖУ на порцию: 500 ккал; "
                            "Б 30 г; Ж 15 г; У 40 г\nИнструкция"
                        ),
                        servings="2",
                    )
                )
        return WeeklyMenuGenerationResult(
            status=WeeklyMenuGenerationStatus.SUCCESS,
            revision_view=SimpleNamespace(entries=tuple(entries)),
        )


def test_disabled_gate_is_local_and_does_not_create_database(tmp_path):
    db_path = tmp_path / "disabled.db"
    controller = build_inventory_telegram_controller(
        env={},
        db_path=db_path,
    )

    result = controller.home(ACTOR)

    assert result.state == "disabled"
    assert result.screen.text == INVENTORY_PLACEHOLDER_REPLY
    assert not db_path.exists()
    assert HEALBITE_REPLY_KEYBOARD_ACTIONS["🥕 Продукты дома"] == INVENTORY_COMMAND


def test_text_review_edit_delete_confirm_and_isolation_state_machine(tmp_path):
    db_path = tmp_path / "inventory.db"
    household = _seed_household(db_path)
    _seed_household(db_path, OTHER_ACTOR)
    controller = _controller(db_path)

    home = controller.home(ACTOR)
    waiting = controller.handle_callback(
        ACTOR,
        _find_callback(home, "Ввести список текстом"),
    )
    assert waiting.state == "awaiting_text"

    review = controller.handle_text(
        ACTOR,
        "рис 1 кг, молоко 1 л, яйца 10 шт, сыр, томаты, огурцы, "
        "яблоки, бананы, хлеб, йогурт",
    )
    assert review is not None and review.state == "review"
    assert review.item_count == 10
    assert controller.pending_input_kind(ACTOR) is None
    assert _find_callback(review, "▶")

    foreign = controller.handle_callback(
        OTHER_ACTOR,
        _find_callback(review, "Подтвердить"),
    )
    assert foreign.state == "stale"

    edit_callback = _find_callback(review, "Изм. 1")
    awaiting_edit = controller.handle_callback(ACTOR, edit_callback)
    assert awaiting_edit.state == "awaiting_edit"
    edited = controller.handle_text(ACTOR, "бурый рис 2 кг")
    assert edited is not None and edited.state == "review"

    stale_after_revision_change = controller.handle_callback(ACTOR, edit_callback)
    assert stale_after_revision_change.state == "stale"

    awaiting_add = controller.handle_callback(
        ACTOR,
        _find_callback(edited, "Добавить"),
    )
    assert awaiting_add.state == "awaiting_add"
    added = controller.handle_text(ACTOR, "кефир 1 л")
    assert added is not None and added.item_count == 11

    delete_callback = _find_callback(added, "Удал. 1")
    deleted = controller.handle_callback(ACTOR, delete_callback)
    assert deleted.state == "deleted"
    assert deleted.item_count == 10
    replay_delete = controller.handle_callback(ACTOR, delete_callback)
    assert replay_delete.state == "stale"

    confirm_callback = _find_callback(deleted, "Подтвердить")
    confirmed = controller.handle_callback(ACTOR, confirm_callback)
    replay_confirm = controller.handle_callback(ACTOR, confirm_callback)
    assert confirmed.state == "confirmed"
    assert replay_confirm.state == "confirmed"
    confirmed_text = "\n".join((confirmed.screen.text, *confirmed.continuations))
    assert "бурый рис" not in confirmed_text
    assert "кефир" in confirmed_text

    store = HealBiteInventoryStore(db_path=db_path)
    scope = InventoryOwnerScope(household_id=household.household.id)
    latest = store.get_latest_confirmed_snapshot(scope)
    assert latest is not None
    assert latest.snapshot.status is InventoryStatus.CONFIRMED
    assert latest.snapshot.id == _snapshot_id(deleted)
    assert (
        controller.handle_callback(ACTOR, _find_callback(deleted, "Изм. 1")).state
        == "stale"
    )


def test_text_ui_rejects_unbounded_item_count(tmp_path):
    db_path = tmp_path / "bounded.db"
    _seed_household(db_path)
    controller = _controller(db_path)
    controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Ввести список"),
    )

    result = controller.handle_text(
        ACTOR,
        ",".join(f"продукт-{index}" for index in range(101)),
    )

    assert result is not None and result.state == "invalid_input"
    assert controller.pending_input_kind(ACTOR) == "text"


def test_callback_revalidates_gate_before_confirmation(tmp_path):
    db_path = tmp_path / "revalidate.db"
    household = _seed_household(db_path)
    controller = _controller(db_path)
    controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Ввести список"),
    )
    review = controller.handle_text(ACTOR, "рис 1 кг")
    assert review is not None
    controller._text_config = _gate(ACTOR, enabled=False)

    blocked = controller.handle_callback(
        ACTOR,
        _find_callback(review, "Подтвердить"),
    )

    assert blocked.state == "disabled"
    view = HealBiteInventoryStore(db_path=db_path).get_snapshot(
        InventoryOwnerScope(household_id=household.household.id),
        _snapshot_id(review),
    )
    assert view.snapshot.status is InventoryStatus.PENDING


def test_cancelled_or_missing_pending_snapshot_cannot_generate(tmp_path):
    db_path = tmp_path / "cancel.db"
    _seed_household(db_path)
    generation = _GenerationService()
    controller = _controller(db_path, generation_service=generation)

    home = controller.home(ACTOR)
    controller.handle_callback(ACTOR, _find_callback(home, "Ввести список"))
    review = controller.handle_text(ACTOR, "рис 1 кг")
    assert review is not None
    confirmation = _find_callback(review, "Подтвердить")
    generation_callback = confirmation.replace(
        "inventory:v1:c:",
        "inventory:v1:g:",
        1,
    )
    cancelled = controller.handle_callback(
        ACTOR,
        _find_callback(review, "Отменить"),
    )

    assert cancelled.state == "cancelled"
    assert controller.handle_callback(ACTOR, generation_callback).state == "stale"
    assert generation.calls == []
    assert (
        controller.handle_callback(
            ACTOR,
            "inventory:v1:r:00000000-0000-4000-8000-000000000000:1:0",
        ).state
        == "stale"
    )


@pytest.mark.asyncio
async def test_photo_candidate_is_one_shot_pending_and_temp_file_is_removed(
    tmp_path,
):
    db_path = tmp_path / "photo.db"
    household = _seed_household(db_path)
    seen_paths: list[Path] = []
    calls = 0

    async def vision(image_path: str, prompt: str):
        nonlocal calls
        calls += 1
        path = Path(image_path)
        assert path.is_file()
        assert "items" in prompt
        seen_paths.append(path)
        return {
            "success": True,
            "analysis": json.dumps({
                "items": [
                    {
                        "name": "молоко",
                        "quantity_value": "1",
                        "unit": "l",
                        "uncertain": True,
                    }
                ]
            }),
        }

    controller = _controller(db_path, vision_analyze_fn=vision)
    home = controller.home(ACTOR)
    controller.handle_callback(ACTOR, _find_callback(home, "фотографию"))

    review = await controller.handle_photo_bytes(ACTOR, b"synthetic-image")

    assert review is not None and review.state == "review"
    assert review.item_count == 1
    assert calls == 1
    assert all(not path.exists() for path in seen_paths)
    parsed = parse_inventory_callback(_find_callback(review, "Подтвердить"))
    assert parsed is not None and parsed.snapshot_id is not None
    view = HealBiteInventoryStore(db_path=db_path).get_snapshot(
        InventoryOwnerScope(household_id=household.household.id),
        parsed.snapshot_id,
    )
    assert view.snapshot.status is InventoryStatus.PENDING
    assert view.items[0].uncertainty == "needs_confirmation"


@pytest.mark.asyncio
async def test_photo_mode_rejects_text_and_revalidates_gate_before_snapshot(
    tmp_path,
):
    db_path = tmp_path / "photo-revalidation.db"
    _seed_household(db_path)
    calls = 0

    async def vision(_image_path: str, _prompt: str):
        nonlocal calls
        calls += 1
        controller._photo_config = _gate(ACTOR, enabled=False)
        return {
            "success": True,
            "analysis": json.dumps({
                "items": [
                    {
                        "name": "молоко",
                        "quantity_value": "1",
                        "unit": "l",
                        "uncertain": False,
                    }
                ]
            }),
        }

    controller = _controller(db_path, vision_analyze_fn=vision)
    controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "фотографию"),
    )

    text_result = controller.handle_text(ACTOR, "не изображение")
    photo_result = await controller.handle_photo_bytes(ACTOR, b"synthetic-image")

    assert text_result is not None and text_result.state == "awaiting_photo"
    assert photo_result is not None and photo_result.state == "disabled"
    assert calls == 1
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name = 'healbite_inventory_snapshots'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_vision_failure_switches_to_safe_text_fallback_without_retry(tmp_path):
    db_path = tmp_path / "vision-failure.db"
    _seed_household(db_path)
    calls = 0

    async def unavailable(_image_path: str, _prompt: str):
        nonlocal calls
        calls += 1
        raise RuntimeError("synthetic provider failure")

    controller = _controller(db_path, vision_analyze_fn=unavailable)
    home = controller.home(ACTOR)
    controller.handle_callback(ACTOR, _find_callback(home, "фотографию"))

    failed = await controller.handle_photo_bytes(ACTOR, b"synthetic-image")
    recovered = controller.handle_text(ACTOR, "рис 1 кг")

    assert failed is not None and failed.state == "vision_unavailable"
    assert calls == 1
    assert controller.pending_input_kind(ACTOR) is None
    assert recovered is not None and recovered.state == "review"


def test_confirmed_snapshot_generates_strict_draft_and_budget_is_bounded(tmp_path):
    db_path = tmp_path / "generation.db"
    _seed_household(db_path)
    generation = _GenerationService(long_titles=True)
    controller = _controller(db_path, generation_service=generation)
    home = controller.home(ACTOR)
    controller.handle_callback(ACTOR, _find_callback(home, "Ввести список"))
    review = controller.handle_text(ACTOR, "рис 5 кг, молоко 7 л")
    assert review is not None
    confirmed = controller.handle_callback(
        ACTOR,
        _find_callback(review, "Подтвердить"),
    )

    generated = controller.handle_callback(
        ACTOR,
        _find_callback(confirmed, "Составить меню"),
    )
    regenerate = _find_callback(generated, "Перегенерировать")
    regenerated = controller.handle_callback(ACTOR, regenerate)
    limited = controller.handle_callback(ACTOR, regenerate)

    assert generated.state == "draft"
    assert regenerated.state == "draft"
    assert limited.state == "generation_limited"
    assert generated.item_count == 21
    assert len(generation.calls) == INVENTORY_MAX_GENERATION_ATTEMPTS
    for args, kwargs in generation.calls:
        assert args[:2] == (ACTOR, WEEK_START)
        assert kwargs["max_entries"] == 21
        assert kwargs["inventory_snapshot_id"]
    chunks = (generated.screen.text, *generated.continuations)
    rendered = "\n".join(chunks)
    assert len(chunks) > 1
    assert all(len(chunk) <= WEEKLY_MENU_MAX_CHUNK_LENGTH for chunk in chunks)
    assert all(f"2026-07-{day:02d}" in rendered for day in range(6, 13))
    assert rendered.index("Завтрак") < rendered.index("Обед") < rendered.index("Ужин")
    assert "КБЖУ на порцию" in rendered
    assert "порций: 2" in rendered


def test_generation_budget_is_atomic_across_concurrent_callbacks(tmp_path):
    db_path = tmp_path / "concurrent-generation.db"
    _seed_household(db_path)
    generation = _GenerationService()
    controller = _controller(db_path, generation_service=generation)
    controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Ввести список"),
    )
    review = controller.handle_text(ACTOR, "рис 1 кг")
    assert review is not None
    confirmed = controller.handle_callback(
        ACTOR,
        _find_callback(review, "Подтвердить"),
    )
    callback_data = _find_callback(confirmed, "Составить меню")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _index: controller.handle_callback(ACTOR, callback_data),
                range(4),
            )
        )

    assert len(generation.calls) == INVENTORY_MAX_GENERATION_ATTEMPTS
    assert [result.state for result in results].count("draft") == 2
    assert [result.state for result in results].count("generation_limited") == 2


@pytest.mark.asyncio
async def test_command_button_and_text_pending_stay_out_of_generic_lane(tmp_path):
    disabled = _controller(
        tmp_path / "disabled-adapter.db",
        text_enabled=False,
        photo_enabled=False,
        weekly_enabled=False,
    )
    adapter = _adapter(disabled)
    button_label = next(
        label
        for label, action in HEALBITE_REPLY_KEYBOARD_ACTIONS.items()
        if action == INVENTORY_COMMAND
    )
    button_update = SimpleNamespace(
        update_id=1,
        message=_message(text=button_label),
        effective_message=None,
    )
    command_update = SimpleNamespace(
        update_id=2,
        message=_message(text=INVENTORY_COMMAND),
        effective_message=None,
    )

    assert await adapter._maybe_handle_healbite_menu_button(
        button_update,
        SimpleNamespace(),
    )
    await adapter._handle_command(command_update, SimpleNamespace())

    assert adapter._send_message_with_thread_fallback.await_count == 2
    assert {
        call.kwargs["text"]
        for call in adapter._send_message_with_thread_fallback.await_args_list
    } == {INVENTORY_PLACEHOLDER_REPLY}
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()

    db_path = tmp_path / "text-adapter.db"
    _seed_household(db_path)
    controller = _controller(db_path)
    controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Ввести список"),
    )
    adapter = _adapter(controller)
    text_update = SimpleNamespace(
        update_id=3,
        message=_message(text="ULTRA_PRIVATE_PRODUCT 1 кг"),
        effective_message=None,
    )

    await adapter._handle_text_message(text_update, SimpleNamespace())

    assert adapter._send_message_with_thread_fallback.await_count == 1
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_photo_uses_memory_download_without_general_cache_or_logs(
    tmp_path,
    caplog,
):
    db_path = tmp_path / "photo-adapter.db"
    _seed_household(db_path)
    vision = AsyncMock(
        return_value={
            "success": True,
            "analysis": json.dumps({
                "items": [
                    {
                        "name": "PRIVATE_VISIBLE_PRODUCT",
                        "quantity_value": None,
                        "unit": None,
                        "uncertain": False,
                    }
                ]
            }),
        }
    )
    controller = _controller(db_path, vision_analyze_fn=vision)
    controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "фотографию"),
    )
    file_obj = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"image"))
    )
    photo = SimpleNamespace(
        file_size=5,
        file_id="PRIVATE_FILE_ID",
        get_file=AsyncMock(return_value=file_obj),
    )
    adapter = _adapter(controller)
    update = SimpleNamespace(
        update_id=4,
        message=_message(photo=[photo]),
        effective_message=None,
    )

    with caplog.at_level(logging.INFO):
        await adapter._handle_media_message(update, SimpleNamespace())

    assert vision.await_count == 1
    adapter._cache_photo_message_to_event.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    adapter._enqueue_text_event.assert_not_called()
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert "PRIVATE_FILE_ID" not in rendered_logs
    assert "PRIVATE_VISIBLE_PRODUCT" not in rendered_logs


@pytest.mark.asyncio
async def test_generation_callback_shows_progress_before_local_generation(tmp_path):
    db_path = tmp_path / "generation-callback.db"
    _seed_household(db_path)
    generation = _GenerationService()
    controller = _controller(db_path, generation_service=generation)
    controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Ввести список"),
    )
    review = controller.handle_text(ACTOR, "рис 1 кг")
    assert review is not None
    confirmed = controller.handle_callback(
        ACTOR,
        _find_callback(review, "Подтвердить"),
    )
    query = SimpleNamespace(
        id="opaque-generation",
        data=_find_callback(confirmed, "Составить меню"),
        from_user=SimpleNamespace(id=ACTOR, first_name="Ignored"),
        message=_message(text="old"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    adapter = _adapter(controller)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query),
        SimpleNamespace(),
    )

    query.answer.assert_awaited_once_with(text="Составляю черновик меню…")
    query.edit_message_text.assert_awaited_once()
    assert len(generation.calls) == 1
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_inventory_callback_is_consumed_locally_without_payload_logging(
    tmp_path,
    caplog,
):
    db_path = tmp_path / "callback-adapter.db"
    _seed_household(db_path)
    controller = _controller(db_path)
    controller.handle_callback(
        ACTOR,
        _find_callback(controller.home(ACTOR), "Ввести список"),
    )
    review = controller.handle_text(ACTOR, "секретный продукт 1 кг")
    assert review is not None
    callback_data = _find_callback(review, "Подтвердить")
    source_message = _message(text="old")
    query = SimpleNamespace(
        id="opaque",
        data=callback_data,
        from_user=SimpleNamespace(id=ACTOR, first_name="Ignored"),
        message=source_message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    update = SimpleNamespace(callback_query=query)
    adapter = _adapter(controller)

    with caplog.at_level(logging.INFO):
        await adapter._handle_callback_query(update, SimpleNamespace())

    query.edit_message_text.assert_awaited_once()
    adapter.handle_message.assert_not_awaited()
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    assert callback_data not in rendered_logs
    assert "секретный продукт" not in rendered_logs
