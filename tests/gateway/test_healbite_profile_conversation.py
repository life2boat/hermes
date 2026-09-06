import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.healbite_profile_conversation import (
    ProfileUpdateValidationError,
    extract_profile_delta,
    validate_profile_delta,
)
from gateway.healbite_user_profile import (
    HealBiteUserProfileStore,
    effective_household_size,
    format_healbite_profile_report,
)


@pytest.fixture
def store(tmp_path):
    store = HealBiteUserProfileStore(tmp_path / "profile.db")
    store.upsert_user_profile(user_id=101, age=35)
    store.upsert_user_profile(user_id=202)
    return store


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Нас дома трое", {"household_size": 3}),
        ("Нас трое", {"household_size": 3}),
        ("У меня аллергия на арахис", {"allergies": ["арахис"]}),
        ("Не люблю рыбу и печень", {"disliked_foods": ["рыбу", "печень"]}),
        (
            "Люблю итальянскую и азиатскую кухню",
            {"preferred_cuisines": ["итальянскую", "азиатскую"]},
        ),
        (
            "Готовлю примерно 4 раза в неделю",
            {"cooking_frequency": "примерно 4 раза в неделю"},
        ),
        ("Бюджет на питание 20000 рублей", {"budget": "20000 рублей"}),
        (
            "Нас двое, не едим грибы и любим грузинскую кухню",
            {
                "household_size": 2,
                "disliked_foods": ["грибы"],
                "preferred_cuisines": ["грузинскую"],
            },
        ),
        (
            "Теперь нас трое и не люблю рыбу",
            {"household_size": 3, "disliked_foods": ["рыбу"]},
        ),
    ],
)
def test_explicit_extraction(text, expected):
    assert extract_profile_delta(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Что приготовить?",
        "Он сказал: нас трое",
        "Нас двое?",
        "Нас трое, но это не мой профиль",
        "Если нас трое",
        "Не люблю рыбу?",
    ],
)
def test_non_assertions_are_noop(text):
    assert extract_profile_delta(text) == {}


def test_roundtrip_preserves_unspecified_and_nutrition(store):
    store.upsert_user_profile(user_id=101, age=35, sex="male")
    store.apply_conversational_delta(
        user_id=101,
        delta={
            "allergies": ["орехи"],
            "preferred_cuisines": ["итальянская"],
            "budget": "старый бюджет",
        },
    )
    store.apply_conversational_delta(
        user_id=101, delta=extract_profile_delta("Теперь нас трое и не люблю рыбу")
    )
    profile = store.get_user_profile(101)
    assert (
        profile.household_size,
        profile.allergies,
        profile.preferences,
        profile.budget,
        profile.age,
    ) == (3, "орехи", "итальянская", "старый бюджет", 35)
    assert store.get_user_profile(202).household_size is None
    assert store.get_user_profile(202).stop_products is None


def test_fallback_and_empty_delta_do_not_write(store):
    with store._connect() as conn:
        before = tuple(conn.iterdump())
    assert store.apply_conversational_delta(user_id=101, delta={}) == {}
    profile = store.get_user_profile(101)
    assert profile.household_size is None
    assert effective_household_size(profile) == effective_household_size(None) == 1
    with store._connect() as conn:
        assert tuple(conn.iterdump()) == before


@pytest.mark.parametrize(
    "delta",
    [
        {"household_size": 0},
        {"household_size": True},
        {"household_size": None},
        {"household_size": 1.5},
        {"allergies": None},
        {"allergies": "nuts"},
        {"allergies": [""]},
        {"budget": None},
        {"user_id": 202},
        {"household_size": 3, "budget": []},
    ],
)
def test_invalid_delta_atomic_no_write(store, delta):
    with store._connect() as conn:
        before = tuple(conn.iterdump())
    with pytest.raises(ProfileUpdateValidationError):
        store.apply_conversational_delta(user_id=101, delta=delta)
    with store._connect() as conn:
        assert tuple(conn.iterdump()) == before


def test_explicit_clear_distinct_from_omitted_or_null(store):
    store.apply_conversational_delta(user_id=101, delta={"allergies": ["арахис"]})
    store.apply_conversational_delta(user_id=101, delta={"budget": "20000 рублей"})
    assert store.get_user_profile(101).allergies == "арахис"
    store.apply_conversational_delta(
        user_id=101, delta=extract_profile_delta("Очисти аллергии")
    )
    assert store.get_user_profile(101).allergies == ""
    with pytest.raises(ProfileUpdateValidationError):
        validate_profile_delta({"allergies": None})


def test_storage_and_russian_report(store):
    store.apply_conversational_delta(
        user_id=101,
        delta={
            "household_size": 2,
            "cooking_frequency": "4 раза в неделю",
            "budget": "20000 рублей",
        },
    )
    profile = store.get_user_profile(101)
    report = format_healbite_profile_report(profile)
    assert "Человек дома: 2" in report
    assert "Как часто готовите: 4 раза в неделю" in report
    assert "Бюджет на питание: 20000 рублей" in report
    assert "household_size" not in report and "telegram_id" not in report
    assert effective_household_size(profile) == 2
    assert "20000" not in repr(store.get_weekly_menu_profile_snapshot(101))


def test_legacy_upgrade_is_additive_and_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    store = HealBiteUserProfileStore(path)
    store.upsert_user_profile(user_id=101, age=35)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE profiles SET allergies='орехи', preferences='кухня', stop_products='рыба'"
        )
        for name in ("household_size", "cooking_frequency", "budget"):
            conn.execute(f"ALTER TABLE profiles DROP COLUMN {name}")
    old = HealBiteUserProfileStore(path, ensure_schema_on_init=False).get_user_profile(
        101
    )
    assert old.household_size is None and effective_household_size(old) == 1
    store._ensure_schema()
    store._ensure_schema()
    profile = store.get_user_profile(101)
    assert (profile.allergies, profile.preferences, profile.stop_products) == (
        "орехи",
        "кухня",
        "рыба",
    )
    assert profile.budget is None and profile.cooking_frequency is None
    with store._connect() as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "enabled,allowed", [(False, True), (True, False), (True, True)]
)
async def test_memory_gates_and_fields(enabled, allowed):
    from gateway.config import Platform
    from gateway.healbite_conversational_memory import (
        HealBiteConversationalMemoryBridge,
    )

    bridge = Mock()
    coordinator = HealBiteConversationalMemoryBridge(
        memory_bridge=bridge, enabled=enabled, allowlist={101 if allowed else 202}
    )
    source = SimpleNamespace(platform=Platform.TELEGRAM, user_id="101")
    await coordinator.sync_profile_preferences(
        source=source,
        preferences={
            "allergies": "арахис",
            "disliked_foods": "рыба",
            "preferred_cuisines": "азиатская",
        },
    )
    assert bridge.upsert_fact.call_count == (3 if enabled and allowed else 0)
    for call in bridge.upsert_fact.call_args_list:
        assert call.kwargs["user_id"] == 101
        assert call.kwargs["entity"] in {"allergy", "dislike", "diet"}
    bridge.reset_mock()
    await coordinator.sync_profile_preferences(
        source=source, preferences={"budget": "private"}
    )
    bridge.upsert_fact.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_route_preserves_profile_when_memory_fails(store, monkeypatch):
    import gateway.platforms.telegram as telegram

    adapter = object.__new__(telegram.TelegramAdapter)
    adapter._is_callback_user_authorized = Mock(return_value=True)
    adapter._send_message_with_thread_fallback = AsyncMock()

    class Runner:
        _healbite_conversational_memory = SimpleNamespace(
            sync_profile_preferences=AsyncMock(side_effect=RuntimeError)
        )

        def receive(self):
            return None

    runner = Runner()
    adapter._message_handler = runner.receive
    monkeypatch.setattr(telegram, "get_default_healbite_user_profile", lambda: store)
    msg = SimpleNamespace(
        text="Нас двое, не едим грибы и любим грузинскую кухню",
        from_user=SimpleNamespace(id=101),
        chat=SimpleNamespace(id=101, type="private"),
    )
    assert await adapter._maybe_handle_healbite_profile_update(msg)
    assert store.get_user_profile(101).household_size == 2
    assert store.get_user_profile(101).stop_products == "грибы"
    assert store.get_user_profile(202).household_size is None
    sent = runner._healbite_conversational_memory.sync_profile_preferences.call_args.kwargs[
        "preferences"
    ]
    assert set(sent) == {"disliked_foods", "preferred_cuisines"}
    msg.chat.id = 202
    assert not await adapter._maybe_handle_healbite_profile_update(msg)
    msg.chat.id = 101
    adapter._is_callback_user_authorized.return_value = False
    assert not await adapter._maybe_handle_healbite_profile_update(msg)


def test_database_failure_rolls_back_all_fields(store):
    with store._connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_profile BEFORE UPDATE ON profiles BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
        before = tuple(conn.iterdump())
    with pytest.raises(sqlite3.IntegrityError):
        store.apply_conversational_delta(
            user_id=101, delta={"household_size": 3, "allergies": ["арахис"]}
        )
    with store._connect() as conn:
        assert tuple(conn.iterdump()) == before


@pytest.mark.parametrize("owner", [True, "101", 0, -1, 303])
def test_invalid_or_unknown_owner_fails_closed(store, owner):
    with store._connect() as conn:
        before = tuple(conn.iterdump())
    with pytest.raises(ProfileUpdateValidationError):
        store.apply_conversational_delta(user_id=owner, delta={"household_size": 2})
    with store._connect() as conn:
        assert tuple(conn.iterdump()) == before
