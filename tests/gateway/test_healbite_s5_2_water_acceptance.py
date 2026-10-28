"""
Milestone S5.2 — Water & Hydration Tracking Acceptance Test Suite.

Verifies:
- AC-01: Water tracker entry point, report format, missing target hint, action buttons.
- AC-02: Quick logging (+250 ml, +500 ml), persistence, idempotency.
- AC-03: Custom amount parsing (300, 300 мл, 0.5 л, 0,5 л, 1 500 мл, 3000), range 1..3000, /cancel.
- AC-04: Undo last entry, user & day scoping, empty history handling.
- AC-05: Timezone-aware local day boundaries and timezone_resolver.
- AC-06: Profile store water target configuration, persistence, and profile report display.
- AC-07: Profile dashboard integration with water target and progress percentage.
- AC-08: Fully deterministic execution (zero LLM / external provider calls).
"""
from __future__ import annotations

import os
import sys

# Ensure local repository root takes precedence
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_user_profile import HealBiteUserProfileStore, parse_profile_field_update
from gateway.healbite_water_tracker import (
    HealBiteWaterTracker,
    WATER_CUSTOM_AMOUNT_STATE,
    WATER_INTAKE_TABLE,
    WATER_TARGET_MISSING_HINT,
    format_water_custom_prompt,
    format_water_tracker_report,
    get_default_water_tracker,
    parse_water_amount,
)
from gateway.platforms.telegram import (
    HEALBITE_PUBLIC_REPLY_KEYBOARD_ROWS,
    HEALBITE_REPLY_KEYBOARD_ACTIONS,
    TelegramAdapter,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={"allowed_users": ["*"]}))
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._enqueue_text_event = Mock()
    return adapter


def _message(*, text: str = "/water", user_id: int = 101, message_id: int = 1):
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        from_user=SimpleNamespace(id=user_id, username="tester", first_name="Tester"),
        chat=SimpleNamespace(id=555, type="private"),
        chat_id=555,
        message_thread_id=None,
    )


def _query(*, user_id: int = 101, query_id: str = "cb-1"):
    message = SimpleNamespace(
        chat_id=555,
        chat=SimpleNamespace(id=555, type="private"),
        message_thread_id=None,
    )
    return SimpleNamespace(
        id=query_id,
        from_user=SimpleNamespace(id=user_id, username="tester", first_name="Tester"),
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# AC-01: Entry point & screen rendering
# ══════════════════════════════════════════════════════════════════════════════

class TestAC01WaterTrackerEntryPoint:
    def test_reply_keyboard_contains_water_tracker_action(self):
        """Reply keyboard maps both modern and legacy water button labels to /water."""
        assert HEALBITE_REPLY_KEYBOARD_ACTIONS.get("💧 Вода") == "/water"
        assert HEALBITE_REPLY_KEYBOARD_ACTIONS.get("💧 Трекер воды") == "/water"
        flat_public_labels = [label for row in HEALBITE_PUBLIC_REPLY_KEYBOARD_ROWS for label in row]
        assert "💧 Вода" in flat_public_labels

    def test_screen_display_with_target(self, tmp_path):
        """Screen displays consumed, target, remaining, progress bar and percent."""
        tracker = HealBiteWaterTracker(
            db_path=tmp_path / "test.db",
            water_target_resolver=lambda uid: 2000,
        )
        tracker.add_water_intake(101, 500)
        summary = tracker.get_water_summary(101)
        report = format_water_tracker_report(summary)

        assert "💧 <b>Вода сегодня</b>" in report
        assert "Выпито: 500 мл" in report
        assert "Цель: 2 000 мл" in report
        assert "Осталось: 1 500 мл" in report
        assert "Прогресс:" in report
        assert "25%" in report
        assert "Записей сегодня: 1" in report
        assert WATER_TARGET_MISSING_HINT not in report

    def test_screen_display_without_target_shows_profile_hint(self, tmp_path):
        """When no target is configured, shows clear explanation directing to /profile."""
        tracker = HealBiteWaterTracker(
            db_path=tmp_path / "test.db",
            water_target_resolver=lambda uid: None,
        )
        tracker.add_water_intake(101, 250)
        summary = tracker.get_water_summary(101)
        report = format_water_tracker_report(summary)

        assert "Выпито: 250 мл" in report
        assert "Цель: —" in report
        assert WATER_TARGET_MISSING_HINT in report
        assert "Заполните /profile" in report

    def test_inline_keyboard_action_buttons(self, monkeypatch):
        """Inline keyboard provides quick adds, custom, undo, refresh, and back."""
        class _FakeBtn:
            def __init__(self, text, callback_data=None, **kwargs):
                self.text = text
                self.callback_data = callback_data

        class _FakeMarkup:
            def __init__(self, inline_keyboard):
                self.inline_keyboard = inline_keyboard

        monkeypatch.setattr("gateway.platforms.telegram.TELEGRAM_AVAILABLE", True)
        monkeypatch.setattr("gateway.platforms.telegram.InlineKeyboardButton", _FakeBtn)
        monkeypatch.setattr("gateway.platforms.telegram.InlineKeyboardMarkup", _FakeMarkup)
        adapter = _adapter()
        kb = adapter._healbite_water_keyboard()
        assert kb is not None
        callbacks = [
            btn.callback_data
            for row in kb.inline_keyboard
            for btn in row
        ]
        assert "water:add:250" in callbacks
        assert "water:add:500" in callbacks
        assert "water:custom" in callbacks
        assert "water:undo" in callbacks
        assert "water:refresh" in callbacks
        assert "water:back" in callbacks


# ══════════════════════════════════════════════════════════════════════════════
# AC-02: Quick water logging
# ══════════════════════════════════════════════════════════════════════════════

class TestAC02QuickWaterLogging:
    @pytest.mark.asyncio
    async def test_quick_add_250_and_500_updates_screen(self, tmp_path, monkeypatch):
        """User can add +250 and +500 ml via inline buttons; screen updates from persisted data."""
        tracker = HealBiteWaterTracker(
            db_path=tmp_path / "test.db",
            water_target_resolver=lambda uid: 2000,
        )
        monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
        adapter = _adapter()

        q1 = _query(user_id=101, query_id="q-add-250")
        await adapter._handle_healbite_water_callback(q1, "water:add:250")
        assert tracker.get_water_intake_today(101) == 250
        q1.answer.assert_awaited_with(text="Добавил 250 мл.")
        text1 = q1.edit_message_text.await_args.kwargs["text"]
        assert "Выпито: 250 мл" in text1

        q2 = _query(user_id=101, query_id="q-add-500")
        await adapter._handle_healbite_water_callback(q2, "water:add:500")
        assert tracker.get_water_intake_today(101) == 750
        q2.answer.assert_awaited_with(text="Добавил 500 мл.")
        text2 = q2.edit_message_text.await_args.kwargs["text"]
        assert "Выпито: 750 мл" in text2

    @pytest.mark.asyncio
    async def test_quick_add_idempotency_prevents_duplicate(self, tmp_path, monkeypatch):
        """Rapid double tap on callback button answers already recorded and does not duplicate."""
        tracker = HealBiteWaterTracker(db_path=tmp_path / "test.db")
        monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
        adapter = _adapter()

        query = _query(user_id=101, query_id="same-tap")
        await adapter._handle_healbite_water_callback(query, "water:add:250")
        await adapter._handle_healbite_water_callback(query, "water:add:250")

        assert tracker.get_water_intake_today(101) == 250
        assert query.answer.await_args_list[1].kwargs["text"] == "Уже записано."


# ══════════════════════════════════════════════════════════════════════════════
# AC-03: Custom water amount flow
# ══════════════════════════════════════════════════════════════════════════════

class TestAC03CustomWaterAmount:
    @pytest.mark.parametrize(
        ("input_text", "expected_ml"),
        [
            ("300", 300),
            ("300 мл", 300),
            ("0.5 л", 500),
            ("0,5 л", 500),
            ("0.5л", 500),
            ("0,5л", 500),
            ("1 л", 1000),
            ("1л", 1000),
            ("1.5 л", 1500),
            ("1,5 л", 1500),
            ("2 литра", 2000),
            ("1 литр", 1000),
            ("250 ml", 250),
            ("1 500 мл", 1500),
            ("2 000", 2000),
            ("3000", 3000),
            ("1", 1),
        ],
    )
    def test_custom_parser_supported_forms(self, input_text, expected_ml):
        assert parse_water_amount(input_text) == expected_ml

    @pytest.mark.parametrize(
        "invalid_text",
        [
            "",
            "   ",
            "0",
            "-10",
            "3001",
            "5000 мл",
            "стакан",
            "кружка",
            "abc",
            "1 литр сока",
            "много",
        ],
    )
    def test_custom_parser_rejects_out_of_bounds_and_ambiguous(self, invalid_text):
        assert parse_water_amount(invalid_text) is None

    @pytest.mark.asyncio
    async def test_custom_flow_stages_state_and_saves_entry(self, tmp_path, monkeypatch):
        """User clicks 'Другой объём', enters custom text, state is cleared, and amount saved."""
        tracker = HealBiteWaterTracker(db_path=tmp_path / "test.db")
        monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
        adapter = _adapter()

        # Step 1: Click custom button
        q = _query(user_id=101)
        await adapter._handle_healbite_water_callback(q, "water:custom")
        assert tracker.get_pending_state(101) == WATER_CUSTOM_AMOUNT_STATE

        # Step 2: Send text reply
        msg = _message(text="0,5 л", user_id=101, message_id=42)
        handled = await adapter._maybe_handle_healbite_water_pending_reply(msg)

        assert handled is True
        assert tracker.get_water_intake_today(101) == 500
        assert tracker.get_pending_state(101) is None
        adapter._enqueue_text_event.assert_not_called()
        sent = adapter._send_message_with_thread_fallback.await_args.kwargs
        assert "Добавил 500 мл." in sent["text"]
        assert "Выпито: 500 мл" in sent["text"]

    @pytest.mark.asyncio
    async def test_custom_flow_cancellation_via_command(self, tmp_path, monkeypatch):
        """Sending /cancel clears pending custom water state without saving intake."""
        tracker = HealBiteWaterTracker(db_path=tmp_path / "test.db")
        tracker.stage_custom_amount(101)
        monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
        adapter = _adapter()

        msg = _message(text="/cancel", user_id=101)
        handled = await adapter._maybe_handle_healbite_water_pending_reply(msg)

        assert handled is True
        assert tracker.get_pending_state(101) is None
        assert tracker.get_water_intake_today(101) == 0
        sent = adapter._send_message_with_thread_fallback.await_args.kwargs
        assert "Ввод воды отменён." in sent["text"]


# ══════════════════════════════════════════════════════════════════════════════
# AC-04: Undo & correction
# ══════════════════════════════════════════════════════════════════════════════

class TestAC04WaterUndo:
    @pytest.mark.asyncio
    async def test_undo_deletes_only_last_today_entry(self, tmp_path, monkeypatch):
        """Undo removes only the latest entry for the current day for this user."""
        tracker = HealBiteWaterTracker(db_path=tmp_path / "test.db")
        tracker.add_water_intake(101, 250, idempotency_key="one")
        tracker.add_water_intake(101, 500, idempotency_key="two")
        tracker.add_water_intake(202, 1000, idempotency_key="other-user")
        monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
        adapter = _adapter()

        q = _query(user_id=101, query_id="undo-cb")
        await adapter._handle_healbite_water_callback(q, "water:undo")

        assert tracker.get_water_intake_today(101) == 250
        assert tracker.get_water_intake_today(202) == 1000
        q.answer.assert_awaited_with(text="Готово.")
        text = q.edit_message_text.await_args.kwargs["text"]
        assert "Удалил последнюю запись: 500 мл." in text
        assert "Выпито: 250 мл" in text

    @pytest.mark.asyncio
    async def test_undo_when_no_entries_handles_gracefully(self, tmp_path, monkeypatch):
        """Undo on empty daily log returns a friendly notice without failing."""
        tracker = HealBiteWaterTracker(db_path=tmp_path / "test.db")
        monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
        adapter = _adapter()

        q = _query(user_id=101, query_id="undo-empty")
        await adapter._handle_healbite_water_callback(q, "water:undo")

        q.answer.assert_awaited_with(text="Записей за сегодня нет.")
        text = q.edit_message_text.await_args.kwargs["text"]
        assert "Сегодня пока нечего отменять." in text


# ══════════════════════════════════════════════════════════════════════════════
# AC-05: Timezone-aware local day window
# ══════════════════════════════════════════════════════════════════════════════

class TestAC05TimezoneHandling:
    def test_timezone_resolver_in_water_tracker(self, tmp_path):
        """Water tracker automatically uses injected timezone_resolver for local-day bounds."""
        timezones = {
            101: "Asia/Vladivostok",  # UTC+10
            202: "Europe/Moscow",     # UTC+3
        }
        tracker = HealBiteWaterTracker(
            db_path=tmp_path / "test.db",
            timezone_resolver=lambda uid: timezones.get(uid),
        )

        # 2026-10-28 15:30 UTC:
        # In Vladivostok (UTC+10): 2026-10-29 01:30 (next calendar day!)
        # In Moscow (UTC+3): 2026-10-28 18:30 (current calendar day!)
        ts_utc = datetime(2026, 10, 28, 15, 30, tzinfo=timezone.utc)

        tracker.add_water_intake(101, 300, consumed_at=ts_utc)
        tracker.add_water_intake(202, 400, consumed_at=ts_utc)

        sum_vlad = tracker.get_water_intake_today(101, now=ts_utc)
        sum_msk = tracker.get_water_intake_today(202, now=ts_utc)

        assert sum_vlad == 300
        assert sum_msk == 400


# ══════════════════════════════════════════════════════════════════════════════
# AC-06: Profile store water target configuration & reporting
# ══════════════════════════════════════════════════════════════════════════════

class TestAC06ProfileWaterTargetIntegration:
    def test_update_water_target_method(self, tmp_path):
        """Profile store can update and retrieve water_target_ml."""
        store = HealBiteUserProfileStore(db_path=tmp_path / "test.db")
        store.upsert_user_profile(user_id=101, username="tester", weight_kg=70)
        assert store.get_water_target_ml(101) is None

        store.update_water_target(101, 2300)
        assert store.get_water_target_ml(101) == 2300

        profile = store.get_user_profile(101)
        assert profile.water_target_ml == 2300

    def test_declarative_text_parses_water_target(self):
        """parse_profile_field_update extracts water target from Russian natural phrasing."""
        assert parse_profile_field_update("цель по воде 2200 мл") == ("water_target_ml", 2200)
        assert parse_profile_field_update("норма воды: 2.5 л") == ("water_target_ml", 2500)
        assert parse_profile_field_update("вода: 2000") == ("water_target_ml", 2000)
        assert parse_profile_field_update("моя норма воды 1.8 л") == ("water_target_ml", 1800)

    def test_update_profile_field_persists_water_target(self, tmp_path):
        """Updating water_target_ml through update_profile_field persists cleanly."""
        store = HealBiteUserProfileStore(db_path=tmp_path / "test.db")
        store.upsert_user_profile(user_id=101, username="tester", weight_kg=75)

        updated, recalculated = store.update_profile_field(user_id=101, field="water_target_ml", value=2400)
        assert recalculated is False  # Nutrition targets not affected
        assert updated.water_target_ml == 2400
        assert store.get_water_target_ml(101) == 2400

    def test_profile_report_renders_water_target_when_configured(self, tmp_path):
        """format_healbite_profile_report includes water target when present."""
        from gateway.healbite_user_profile import format_healbite_profile_report
        store = HealBiteUserProfileStore(db_path=tmp_path / "test.db")
        store.upsert_user_profile(
            user_id=101,
            username="tester",
            daily_kcal_target=2100,
            daily_protein_target=140,
            daily_fat_target=70,
            daily_carbs_target=230,
            water_target_ml=2200,
        )
        report = format_healbite_profile_report(store.get_user_profile(101))
        assert "💧 Вода: 2200 мл" in report


# ══════════════════════════════════════════════════════════════════════════════
# AC-07: Profile dashboard integration
# ══════════════════════════════════════════════════════════════════════════════

class TestAC07DashboardIntegration:
    def test_dashboard_displays_water_progress(self, tmp_path, monkeypatch):
        """Dashboard renders water progress line using get_default_water_tracker."""
        store = HealBiteUserProfileStore(db_path=tmp_path / "test.db")
        store.upsert_user_profile(
            user_id=101,
            username="tester",
            daily_kcal_target=2000,
            water_target_ml=2000,
        )
        tracker = HealBiteWaterTracker(
            db_path=tmp_path / "test.db",
            water_target_resolver=store.get_water_target_ml,
        )
        tracker.add_water_intake(101, 1000)

        monkeypatch.setattr("gateway.healbite_water_tracker.get_default_water_tracker", lambda *a, **kw: tracker)
        monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda *a, **kw: tracker)
        monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

        adapter = _adapter()
        dashboard_text = adapter._build_healbite_returning_dashboard(user_id=101)

        assert "💧 <b>Вода сегодня:</b> 1 000 мл / 2 000 мл (50%)" in dashboard_text


# ══════════════════════════════════════════════════════════════════════════════
# AC-08: Zero LLM / Zero Provider Guarantee
# ══════════════════════════════════════════════════════════════════════════════

class TestAC08DeterministicExecution:
    @pytest.mark.asyncio
    async def test_entire_water_lifecycle_uses_zero_llm_calls(self, tmp_path, monkeypatch):
        """Logging, custom entry, undo, and report formatting execute without any LLM invocations."""
        tracker = HealBiteWaterTracker(
            db_path=tmp_path / "test.db",
            water_target_resolver=lambda uid: 2000,
        )
        monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
        adapter = _adapter()

        # 1. Open water screen
        handled_open = await adapter._maybe_handle_healbite_water_command(_message(text="/water", user_id=101))
        assert handled_open is True

        # 2. Add 250 ml
        await adapter._handle_healbite_water_callback(_query(user_id=101, query_id="tap-1"), "water:add:250")

        # 3. Custom amount 500 ml
        await adapter._handle_healbite_water_callback(_query(user_id=101, query_id="tap-custom"), "water:custom")
        await adapter._maybe_handle_healbite_water_pending_reply(_message(text="0.5 л", user_id=101))

        # 4. Undo last
        await adapter._handle_healbite_water_callback(_query(user_id=101, query_id="tap-undo"), "water:undo")

        # Verify zero text events queued to LLM agent pipeline
        adapter._enqueue_text_event.assert_not_called()
        assert tracker.get_water_intake_today(101) == 250
