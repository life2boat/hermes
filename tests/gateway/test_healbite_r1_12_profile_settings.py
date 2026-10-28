"""Tests for HealBite R1-12 Profile Settings, Preferences, and Reset (S5.0)."""

from datetime import datetime, timezone
import pytest

from gateway.healbite_nutrition_targets import (
    ACTIVITY_LEVEL_DESCRIPTIONS_RU,
    CALCULATION_METHOD_LABELS_RU,
    SUPPORTED_CALCULATION_METHODS,
    NutritionTargetValidationError,
    activity_level_description,
    activity_level_label,
    calculation_method_label,
    normalize_activity_level,
    validate_calculation_method,
)
from gateway.healbite_nutrition_diary import HealBiteNutritionDiary
from gateway.healbite_time import (
    COMMON_CITY_TIMEZONES,
    DEFAULT_TIMEZONE_NAME,
    OFFSET_TIMEZONES,
    format_local_now,
    local_day_window_utc,
    normalize_timezone_name,
    validate_timezone,
)
from gateway.healbite_user_profile import (
    HealBiteUserProfileStore,
    UserDeleteBlockedActiveHouseholdRelationError,
    format_healbite_profile_report,
    format_healbite_settings_report,
    parse_profile_field_update,
)


@pytest.fixture
def test_db_path(tmp_path):
    return tmp_path / "test_healbite_r1_12.db"


@pytest.fixture
def profile_store(test_db_path):
    return HealBiteUserProfileStore(test_db_path)


@pytest.fixture
def diary_store(test_db_path):
    return HealBiteNutritionDiary(test_db_path)


def _insert_meal(diary_store, user_id, meal_name, calories, protein, fat, carbs, occurred_at=None):
    with diary_store._connect() as conn:
        occ = occurred_at.strftime("%Y-%m-%d %H:%M:%S") if occurred_at else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """
            INSERT INTO nutrition_log (user_id, source, meal_name, raw_summary, items_json, calories_kcal, protein_g, fat_g, carbs_g, occurred_at, created_at)
            VALUES (?, 'text', ?, ?, '[]', ?, ?, ?, ?, ?, ?)
            """,
            (int(user_id), meal_name, meal_name, calories, protein, fat, carbs, occ, occ),
        )


# ==============================================================================
# 1. Location & Timezone Settings and Persistence
# ==============================================================================

def test_timezone_setting_and_persistence(profile_store):
    user_id = 1001
    profile_store.upsert_user_profile(
        user_id=user_id,
        username="alice",
        sex="female",
        age=28,
        height_cm=168.0,
        weight_kg=60.0,
        goal="maintenance",
        activity_level="1.375",
        timezone="Asia/Yekaterinburg",
        location="Екатеринбург",
    )
    profile = profile_store.get_user_profile(user_id)
    assert profile.timezone == "Asia/Yekaterinburg"
    assert profile.location == "Екатеринбург"

    # Update via helper
    updated = profile_store.update_location_and_timezone(user_id, location="Москва", timezone_name="Europe/Moscow")
    assert updated.timezone == "Europe/Moscow"
    assert updated.location == "Москва"


def test_city_mapping_to_timezone(profile_store):
    user_id = 1002
    profile_store.upsert_user_profile(user_id=user_id, sex="male", age=30, height_cm=180, weight_kg=80)
    
    # Passing Russian city should automatically map to IANA timezone
    updated = profile_store.update_location_and_timezone(user_id, location="Владивосток")
    assert updated.location == "Владивосток"
    assert updated.timezone == "Asia/Vladivostok"

    updated2 = profile_store.update_location_and_timezone(user_id, location="Калининград")
    assert updated2.location == "Калининград"
    assert updated2.timezone == "Europe/Kaliningrad"


def test_timezone_validation_fail_closed():
    # Valid timezones
    assert validate_timezone("Europe/Moscow") == "Europe/Moscow"
    assert validate_timezone("Asia/Vladivostok") == "Asia/Vladivostok"
    assert validate_timezone("UTC") == "UTC"
    assert validate_timezone("москва") == "Europe/Moscow"
    assert validate_timezone("+3") == "Europe/Moscow"
    assert validate_timezone("+5") == "Asia/Yekaterinburg"
    assert validate_timezone("MSK") == "Europe/Moscow"

    # Invalid timezones must raise ValueError
    with pytest.raises(ValueError):
        validate_timezone("Mars/Olympus")

    with pytest.raises(ValueError):
        validate_timezone("Invalid/Zone")

    with pytest.raises(ValueError):
        validate_timezone("12345")


# ==============================================================================
# 2. Timezone-Aware Day Boundaries in Food Diary
# ==============================================================================

def test_local_day_window_utc_boundaries():
    # Wednesday 2026-10-28 01:00 UTC
    # In Europe/Moscow (UTC+3): 04:00 on 2026-10-28
    # Day start in Moscow: 2026-10-28 00:00 MSK = 2026-10-27 21:00 UTC
    # Day end in Moscow: 2026-10-29 00:00 MSK = 2026-10-28 21:00 UTC
    ref_utc = datetime(2026, 10, 28, 1, 0, 0, tzinfo=timezone.utc)
    start_utc, end_utc = local_day_window_utc(ref_utc, "Europe/Moscow")
    assert start_utc == datetime(2026, 10, 27, 21, 0, 0, tzinfo=timezone.utc)
    assert end_utc == datetime(2026, 10, 28, 21, 0, 0, tzinfo=timezone.utc)

    # In Asia/Vladivostok (UTC+10): 11:00 on 2026-10-28
    # Day start in Vladivostok: 2026-10-28 00:00 = 2026-10-27 14:00 UTC
    # Day end in Vladivostok: 2026-10-29 00:00 = 2026-10-28 14:00 UTC
    start_vlad, end_vlad = local_day_window_utc(ref_utc, "Asia/Vladivostok")
    assert start_vlad == datetime(2026, 10, 27, 14, 0, 0, tzinfo=timezone.utc)
    assert end_vlad == datetime(2026, 10, 28, 14, 0, 0, tzinfo=timezone.utc)


def test_food_diary_timezone_resolution(diary_store, profile_store):
    user_id = 1003
    # User is in Vladivostok (UTC+10)
    profile_store.upsert_user_profile(
        user_id=user_id,
        sex="male",
        age=30,
        height_cm=180,
        weight_kg=80,
        timezone="Asia/Vladivostok",
        location="Владивосток",
    )

    # Log a meal at 2026-10-27 15:00 UTC (which is 2026-10-28 01:00 in Vladivostok, but 18:00 on 10-27 in Moscow)
    meal_time = datetime(2026, 10, 27, 15, 0, 0, tzinfo=timezone.utc)
    _insert_meal(
        diary_store,
        user_id=user_id,
        meal_name="Ночной перекус во Владивостоке",
        calories=450,
        protein=20,
        fat=15,
        carbs=50,
        occurred_at=meal_time,
    )

    # When querying summary at 2026-10-28 02:00 UTC (12:00 Vladivostok time), the meal must be counted in TODAY's summary for Vladivostok
    query_time = datetime(2026, 10, 28, 2, 0, 0, tzinfo=timezone.utc)
    summary = diary_store.get_summary(user_id=user_id, now=query_time, days=1)
    assert len(summary["entries"]) == 1
    assert summary["entries"][0]["meal_name"] == "Ночной перекус во Владивостоке"
    assert summary["calories_kcal"] == 450


# ==============================================================================
# 3. Privacy & Zero Device GPS Collection
# ==============================================================================

def test_zero_device_gps_collection(profile_store):
    # Location is strictly an administrative text string or city alias; no lat/long coordinates are required or stored.
    user_id = 1004
    profile_store.upsert_user_profile(
        user_id=user_id,
        location="Казань",
        timezone="Europe/Moscow",
    )
    profile = profile_store.get_user_profile(user_id)
    assert profile.location == "Казань"
    assert not hasattr(profile, "latitude")
    assert not hasattr(profile, "longitude")


# ==============================================================================
# 4. Referral & Invitation Tracking
# ==============================================================================

def test_referral_tracking_and_constraints(profile_store):
    alice_id = 2001
    bob_id = 2002
    charlie_id = 2003

    # Link generation
    link = profile_store.get_referral_link(alice_id, "HealBitebot")
    assert link == "https://t.me/HealBitebot?start=ref_2001"

    # Initial stats
    assert profile_store.get_referral_count(alice_id) == 0

    # Alice refers Bob
    recorded_bob = profile_store.record_referral(referrer_id=alice_id, referred_id=bob_id)
    assert recorded_bob is True
    assert profile_store.get_referral_count(alice_id) == 1

    # Duplicate referral attribution must be rejected (Bob already referred)
    duplicate_bob = profile_store.record_referral(referrer_id=alice_id, referred_id=bob_id)
    assert duplicate_bob is False

    # Another user trying to refer Bob must also be rejected
    other_ref_bob = profile_store.record_referral(referrer_id=charlie_id, referred_id=bob_id)
    assert other_ref_bob is False

    # Alice cannot refer herself
    self_ref = profile_store.record_referral(referrer_id=alice_id, referred_id=alice_id)
    assert self_ref is False

    # Alice refers Charlie
    recorded_charlie = profile_store.record_referral(referrer_id=alice_id, referred_id=charlie_id)
    assert recorded_charlie is True
    assert profile_store.get_referral_count(alice_id) == 2


# ==============================================================================
# 5. Dietary Preferences & Non-Destructive Editing
# ==============================================================================

def test_dietary_preferences_editing(profile_store):
    user_id = 3001
    profile_store.upsert_user_profile(
        user_id=user_id,
        sex="female",
        age=25,
        height_cm=165,
        weight_kg=55,
        goal="maintenance",
        activity_level="1.2",
    )

    # Set initial preferences
    p1 = profile_store.update_dietary_preferences(
        user_id=user_id,
        diet_type="Кето",
        allergies="Арахис, лактоза",
        stop_products="Кинза",
        preferences="Итальянская кухня",
    )
    assert p1.diet_type == "Кето"
    assert p1.allergies == "Арахис, лактоза"
    assert p1.stop_products == "Кинза"
    assert p1.preferences == "Итальянская кухня"

    # Non-destructive edit: update only allergies, rest remains untouched
    p2 = profile_store.update_dietary_preferences(
        user_id=user_id,
        allergies="Арахис, лактоза, соя",
    )
    assert p2.diet_type == "Кето"
    assert p2.allergies == "Арахис, лактоза, соя"
    assert p2.stop_products == "Кинза"
    assert p2.preferences == "Итальянская кухня"

    # Clearing stop_products explicitly
    p3 = profile_store.update_dietary_preferences(
        user_id=user_id,
        stop_products="очистить",
    )
    assert p3.stop_products is None
    assert p3.allergies == "Арахис, лактоза, соя"
    assert p3.diet_type == "Кето"


# ==============================================================================
# 6. Immutable Initial Profile Measurements
# ==============================================================================

def test_immutable_initial_measurements(profile_store):
    user_id = 4001
    # User onboards with initial weight 80 kg, age 30, height 180 cm
    profile = profile_store.upsert_user_profile(
        user_id=user_id,
        sex="male",
        age=30,
        height_cm=180.0,
        weight_kg=80.0,
        goal="weight_loss",
        activity_level="1.375",
    )
    assert profile.weight_kg == 80.0
    assert profile.initial_weight_kg == 80.0
    assert profile.initial_age == 30
    assert profile.initial_height_cm == 180.0

    # User subsequently logs weight change: now 76 kg
    updated = profile_store.upsert_user_profile(
        user_id=user_id,
        weight_kg=76.0,
    )
    assert updated.weight_kg == 76.0
    # Baseline MUST remain frozen at 80.0 kg!
    assert updated.initial_weight_kg == 80.0

    # User updates age next year: now 31
    updated_age = profile_store.upsert_user_profile(
        user_id=user_id,
        age=31,
    )
    assert updated_age.age == 31
    assert updated_age.initial_age == 30

    # Report format includes baseline weight comparison
    report = format_healbite_profile_report(updated_age)
    assert "76 кг (на старте: 80 кг)" in report


# ==============================================================================
# 7. Supported Calorie-Calculation Method Selection
# ==============================================================================

def test_calculation_method_selection(profile_store):
    user_id = 5001
    profile_store.upsert_user_profile(
        user_id=user_id,
        sex="female",
        age=30,
        height_cm=170.0,
        weight_kg=65.0,
        goal="weight_loss",
        activity_level="1.375",
    )

    # Default / standard is mifflin_v1
    assert validate_calculation_method("mifflin_v1") == "mifflin_v1"
    assert validate_calculation_method("миффлин") == "mifflin_v1"
    assert validate_calculation_method("стандарт") == "mifflin_v1"
    assert validate_calculation_method("manual") == "manual"
    assert validate_calculation_method("ручной") == "manual"

    # Unsupported methods must fail closed
    with pytest.raises(NutritionTargetValidationError):
        validate_calculation_method("harris_benedict")

    with pytest.raises(NutritionTargetValidationError):
        validate_calculation_method("keto_formula")

    # Set calculation method to manual
    p_manual = profile_store.set_calculation_method(user_id, "manual")
    assert p_manual.calculation_method == "manual"
    assert p_manual.target_source == "manual"

    # Switch back to mifflin_v1
    p_mifflin = profile_store.set_calculation_method(user_id, "mifflin_v1")
    assert p_mifflin.calculation_method == "mifflin_v1"
    assert p_mifflin.target_source == "calculated"
    assert p_mifflin.daily_kcal_target is not None


# ==============================================================================
# 8. Activity Level Editing & Conflict Resolution with Manual Targets
# ==============================================================================

def test_activity_levels_and_descriptions():
    # Levels 0 to 4 must all be supported and have descriptive Russian explanations
    assert normalize_activity_level("0") == "sedentary"
    assert normalize_activity_level("1") == "light"
    assert normalize_activity_level("2") == "moderate"
    assert normalize_activity_level("3") == "high"
    assert normalize_activity_level("4") == "very_high"

    for level in ("0", "1", "2", "3", "4", "1.2", "1.375", "1.55", "1.725", "1.9"):
        desc = activity_level_description(level)
        assert desc is not None and len(desc) > 10


def test_activity_level_recalculation_for_calculated_profile(profile_store):
    user_id = 6001
    profile_store.upsert_user_profile(
        user_id=user_id,
        sex="male",
        age=30,
        height_cm=180.0,
        weight_kg=80.0,
        goal="maintenance",
        activity_level="1.2",
    )
    profile = profile_store.recalculate_profile_targets(user_id=user_id, target_source="calculated")
    initial_kcal = profile.daily_kcal_target

    # Change activity from level 0 to level 3 (high activity)
    updated, calc, is_conflict = profile_store.update_activity_level(user_id, "3")
    assert not is_conflict
    assert calc is None
    assert updated.activity_level == "high"
    # Target must have automatically recalculated higher!
    assert updated.daily_kcal_target > initial_kcal


def test_activity_level_preserves_manual_targets_on_conflict(profile_store):
    user_id = 6002
    # User with explicit manual targets (e.g. 2500 kcal)
    profile_store.upsert_user_profile(
        user_id=user_id,
        sex="male",
        age=30,
        height_cm=180.0,
        weight_kg=80.0,
        goal="maintenance",
        activity_level="1.2",
    )
    profile_store.set_manual_nutrition_targets(user_id=user_id, calories_kcal=2500, protein_g=160, fat_g=80, carbs_g=285)
    p_before = profile_store.get_user_profile(user_id)
    assert p_before.target_source == "manual"
    assert p_before.daily_kcal_target == 2500.0

    # User changes activity level
    updated, calc, is_conflict = profile_store.update_activity_level(user_id, "3")
    # Conflict must be flagged!
    assert is_conflict is True
    # Reference calculation must be returned
    assert calc is not None
    # BUT manual targets must NOT be silently overwritten!
    assert updated.daily_kcal_target == 2500.0
    assert updated.target_source == "manual"
    assert updated.activity_level == "high"


# ==============================================================================
# 9. Safe Profile Reset & Re-Registration
# ==============================================================================

def test_safe_profile_reset_preserves_diary(profile_store, diary_store):
    user_id = 7001
    profile_store.upsert_user_profile(
        user_id=user_id,
        sex="male",
        age=30,
        height_cm=180.0,
        weight_kg=80.0,
        goal="weight_loss",
        activity_level="1.375",
    )
    # Log meal
    _insert_meal(
        diary_store,
        user_id=user_id,
        meal_name="Обед до сброса",
        calories=600,
        protein=35,
        fat=20,
        carbs=70,
    )

    # Perform profile reset
    profile_store.reset_user_profile(user_id)

    # Profile must be gone, allowing fresh /start onboarding
    assert profile_store.get_user_profile(user_id) is None
    assert profile_store.get_onboarding_state(user_id) is None

    # Diary entries MUST be preserved
    summary = diary_store.get_summary(user_id=user_id, days=1)
    assert len(summary["entries"]) == 1
    assert summary["entries"][0]["meal_name"] == "Обед до сброса"

    # Diary entries MUST be preserved
    summary = diary_store.get_summary(user_id=user_id, days=1)
    assert len(summary["entries"]) == 1
    assert summary["entries"][0]["meal_name"] == "Обед до сброса"


# ==============================================================================
# 10. Natural Language Field Updates & Reports
# ==============================================================================

def test_parse_profile_field_update():
    assert parse_profile_field_update("город Москва") == ("location", "москва")
    assert parse_profile_field_update("часовой пояс Europe/Moscow") == ("timezone", "europe/moscow")
    assert parse_profile_field_update("диета кето") == ("diet_type", "кето")
    assert parse_profile_field_update("аллергия на орехи") == ("allergies", "на орехи")
    assert parse_profile_field_update("стоп-продукты лук, кинза") == ("stop_products", "лук, кинза")
    assert parse_profile_field_update("предпочтения итальянская кухня") == ("preferences", "итальянская кухня")
    assert parse_profile_field_update("рост 175") == ("height_cm", 175.0)
    assert parse_profile_field_update("мне 29 лет") == ("age", 29)


def test_settings_report_formatting(profile_store):
    user_id = 8001
    profile = profile_store.upsert_user_profile(
        user_id=user_id,
        sex="female",
        age=27,
        height_cm=165.0,
        weight_kg=58.0,
        goal="maintenance",
        activity_level="1.375",
        location="Казань",
        timezone="Europe/Moscow",
        diet_type="Средиземноморская",
        allergies="Мёд",
    )
    report = format_healbite_settings_report(profile, referral_count=3, referral_link="https://t.me/HealBitebot?start=ref_8001")
    assert "Казань" in report
    assert "Europe/Moscow" in report
    assert "Средиземноморская" in report
    assert "Мёд" in report
    assert "Приглашено друзей:" in report
    assert "3" in report
    assert "https://t.me/HealBitebot?start=ref_8001" in report


# ==============================================================================
# 11. Telegram Handlers & UI Integration
# ==============================================================================

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from gateway.platforms.telegram import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._enqueue_text_event = Mock()
    adapter._should_process_message = lambda msg, is_command=False: True
    adapter._maybe_handle_healbite_menu_button = AsyncMock(return_value=False)
    adapter._apply_telegram_group_observe_attribution = lambda event: event
    adapter._build_message_event = lambda msg, message_type, update_id=None: SimpleNamespace(
        text=getattr(msg, "text", ""),
        message_type=message_type,
        media_types=[],
    )
    adapter._clean_bot_trigger_text = lambda text: text
    adapter._largest_photo_size = lambda msg: None
    adapter._cache_photo_message_to_event = AsyncMock()
    adapter._cache_image_document_message_to_event = AsyncMock()
    adapter._link_preview_kwargs = lambda: {}
    adapter._healbite_main_menu_keyboard = lambda: []
    adapter._healbite_reply_keyboard = lambda rows: rows
    adapter._should_observe_unmentioned_group_message = lambda msg: False
    adapter._observe_unmentioned_group_message = Mock()
    adapter.config = SimpleNamespace(extra={"allowed_users": ["*"]})
    adapter._is_telegram_user_authorized = lambda *args, **kwargs: True
    adapter._healbite_public_lane_enabled_for_user = lambda *args, **kwargs: True
    adapter._is_callback_user_authorized = lambda *args, **kwargs: True
    adapter._healbite_sender_username = lambda msg: getattr(getattr(msg, "from_user", None), "username", "")
    adapter._log_healbite_route_selected = lambda *args, **kwargs: None
    adapter._log_healbite_marker = lambda *args, **kwargs: None
    return adapter


def _patch_profile_store(monkeypatch, store: HealBiteUserProfileStore):
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.platforms.telegram.get_existing_healbite_user_profile", lambda: store)


@pytest.mark.asyncio
async def test_telegram_settings_command(profile_store, monkeypatch):
    adapter = _make_adapter()
    _patch_profile_store(monkeypatch, profile_store)
    user_id = 9001
    profile_store.upsert_user_profile(user_id=user_id, sex="male", age=30, height_cm=180, weight_kg=80, goal="maintain", activity_level="moderate")

    msg = SimpleNamespace(
        text="/settings",
        chat=SimpleNamespace(id=user_id, type="private"),
        from_user=SimpleNamespace(id=user_id, username="testuser"),
        message_thread_id=None,
    )
    handled = await adapter._maybe_handle_healbite_settings_command(msg)
    assert handled is True
    call_args = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Настройки профиля" in call_args["text"]
    assert call_args["reply_markup"] is not None


@pytest.mark.asyncio
async def test_telegram_invite_command(profile_store, monkeypatch):
    adapter = _make_adapter()
    _patch_profile_store(monkeypatch, profile_store)
    user_id = 9002

    msg = SimpleNamespace(
        text="/invite",
        chat=SimpleNamespace(id=user_id, type="private"),
        from_user=SimpleNamespace(id=user_id, username="inviter"),
        message_thread_id=None,
    )
    handled = await adapter._maybe_handle_healbite_invite_command(msg)
    assert handled is True
    call_args = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "ref_9002" in call_args["text"]


@pytest.mark.asyncio
async def test_telegram_reset_profile_command(profile_store, monkeypatch):
    adapter = _make_adapter()
    _patch_profile_store(monkeypatch, profile_store)
    user_id = 9003

    msg = SimpleNamespace(
        text="/reset_profile",
        chat=SimpleNamespace(id=user_id, type="private"),
        from_user=SimpleNamespace(id=user_id, username="resetter"),
        message_thread_id=None,
    )
    handled = await adapter._maybe_handle_healbite_reset_profile_command(msg)
    assert handled is True
    call_args = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Сброс профиля" in call_args["text"]


@pytest.mark.asyncio
async def test_telegram_start_with_referral(profile_store, monkeypatch):
    adapter = _make_adapter()
    _patch_profile_store(monkeypatch, profile_store)
    referrer_id = 9010
    new_user_id = 9011

    msg = SimpleNamespace(
        text=f"/start ref_{referrer_id}",
        chat=SimpleNamespace(id=new_user_id, type="private"),
        from_user=SimpleNamespace(id=new_user_id, username="newbie"),
        message_thread_id=None,
    )
    await adapter._maybe_handle_healbite_start_command(msg)
    assert profile_store.get_referral_count(referrer_id) == 1


@pytest.mark.asyncio
async def test_telegram_settings_callbacks(profile_store, monkeypatch):
    adapter = _make_adapter()
    _patch_profile_store(monkeypatch, profile_store)
    user_id = 9020
    profile_store.upsert_user_profile(user_id=user_id, sex="male", age=30, height_cm=180, weight_kg=80, goal="maintain", activity_level="sedentary")

    async def _send_cb(data):
        query = SimpleNamespace(
            from_user=SimpleNamespace(id=user_id, username="cbuser"),
            message=SimpleNamespace(chat=SimpleNamespace(id=user_id, type="private"), message_thread_id=None),
            answer=AsyncMock(),
        )
        await adapter._handle_healbite_settings_callback(query, data)

    # 1. Activity setting callback
    await _send_cb(f"act_set:2:{user_id}")
    profile = profile_store.get_user_profile(user_id)
    assert profile.activity_level == "moderate"

    # 2. Timezone setting callback
    await _send_cb(f"tz_set:Asia/Yekaterinburg:{user_id}")
    profile = profile_store.get_user_profile(user_id)
    assert profile.timezone == "Asia/Yekaterinburg"

    # 3. Calculation method callback
    await _send_cb(f"calc_set:manual:{user_id}")
    profile = profile_store.get_user_profile(user_id)
    assert profile.calculation_method == "manual"
    assert profile.target_source == "manual"

    # 4. Dietary clear callback
    profile_store.update_dietary_preferences(user_id, allergies="Орехи", stop_products="Лук")
    await _send_cb(f"pref:clear:{user_id}")
    profile = profile_store.get_user_profile(user_id)
    assert profile.allergies is None
    assert profile.stop_products is None

    # 5. Reset confirm callback
    await _send_cb(f"reset_do:confirm:{user_id}")
    assert profile_store.get_user_profile(user_id) is None
