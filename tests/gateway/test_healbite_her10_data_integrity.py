import json
import sqlite3
from datetime import datetime, timedelta, timezone
import pytest

from gateway.healbite_food_composition import (
    _FOOD_CATALOG,
    FOOD_COMPOSITION_PROVENANCE,
    FOOD_COMPOSITION_VERSION,
    resolve_food_composition,
)
from gateway.healbite_nutrition_diary import (
    FoodVisionInventory,
    FoodVisionItem,
    HealBiteNutritionDiary,
    PendingFoodVisionInventory,
    PendingFoodVisionItem,
    calculate_inventory_nutrition,
    format_nutrition_diary_report,
)


def _install_target_tables(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                telegram_id INTEGER PRIMARY KEY,
                calories_limit REAL
            );
            CREATE TABLE IF NOT EXISTS structured_user_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                fact_key TEXT NOT NULL,
                fact_value TEXT NOT NULL,
                trust_score REAL DEFAULT 1,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )


def _seed_targets(
    db_path,
    *,
    user_id: int,
    calories_kcal: float | None = None,
    protein_g: float | None = None,
    fat_g: float | None = None,
    carbs_g: float | None = None,
):
    _install_target_tables(db_path)
    with sqlite3.connect(db_path) as conn:
        if calories_kcal is not None:
            conn.execute(
                "INSERT OR REPLACE INTO profiles(telegram_id, calories_limit) VALUES (?, ?)",
                (user_id, calories_kcal),
            )

        target_rows = {
            "target_calories": calories_kcal,
            "target_protein_g": protein_g,
            "target_fat_g": fat_g,
            "target_carbs_g": carbs_g,
        }
        for fact_key, fact_value in target_rows.items():
            if fact_value is None:
                continue
            conn.execute(
                "INSERT INTO structured_user_facts(user_id, fact_key, fact_value, trust_score) VALUES (?, ?, ?, ?)",
                (user_id, fact_key, str(fact_value), 1.0),
            )


def _pending_item(
    index: int,
    visible_name: str,
    normalized_name: str,
    grams: float | None = None,
    min_g: float | None = 100.0,
    max_g: float | None = 100.0,
) -> PendingFoodVisionItem:
    if grams is None:
        min_g = None
        max_g = None
    return PendingFoodVisionItem(
        index=index,
        visible_name=visible_name,
        normalized_name=normalized_name,
        confidence=0.95,
        estimated_grams_min=min_g,
        estimated_grams_max=max_g,
        selected_grams=grams,
        preparation="",
        is_sauce=False,
        uncertainty="",
        user_modified=False,
    )


def _pending_inventory(user_id: int, *items: PendingFoodVisionItem) -> PendingFoodVisionInventory:
    now = datetime.now(timezone.utc)
    return PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id=f"inv-{user_id}",
        schema_version="food_vision_inventory_v1",
        items=list(items),
        overall_confidence=0.95,
        warnings=[],
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        source="vision",
        state="PENDING_INVENTORY_CONFIRMATION",
        image_ref=f"telegram:{user_id}:1",
        occurred_at=now,
    )


def test_dairy_fat_percentages_distinct_profiles():
    """
    Requirement B.1: Dairy fat percentages have distinct, non-overlapping profiles.
    - Творог 5% vs Творог 9%
    - Сметана 15% vs Сметана 20%
    - Молоко 2.5% vs Молоко 3.2%
    Never silently substituted or cross-mapped!
    """
    # 1. Творог
    res_tvorog_5 = resolve_food_composition("творог 5%")
    res_tvorog_9 = resolve_food_composition("творог 9%")

    assert res_tvorog_5.matched is True
    assert res_tvorog_9.matched is True
    assert res_tvorog_5.profile.canonical_id == "cottage_cheese_5pct"
    assert res_tvorog_9.profile.canonical_id == "cottage_cheese_9pct"
    assert res_tvorog_5.profile.fat_g == 5.0
    assert res_tvorog_9.profile.fat_g == 9.0
    assert res_tvorog_5.profile.calories_kcal == 121.0
    assert res_tvorog_9.profile.calories_kcal == 159.0
    assert res_tvorog_5.source_id == "SKURIKHIN-2008-0105"
    assert res_tvorog_9.source_id == "SKURIKHIN-2008-0104"

    # 2. Сметана
    res_smetana_15 = resolve_food_composition("сметана 15%")
    res_smetana_20 = resolve_food_composition("сметана 20%")

    assert res_smetana_15.matched is True
    assert res_smetana_20.matched is True
    assert res_smetana_15.profile.canonical_id == "sour_cream_15pct"
    assert res_smetana_20.profile.canonical_id == "sour_cream_20pct"
    assert res_smetana_15.profile.fat_g == 15.0
    assert res_smetana_20.profile.fat_g == 20.0
    assert res_smetana_15.profile.calories_kcal == 158.0
    assert res_smetana_20.profile.calories_kcal == 206.0
    assert res_smetana_15.source_id == "SKURIKHIN-2008-0112"
    assert res_smetana_20.source_id == "SKURIKHIN-2008-0113"

    # 3. Молоко
    res_milk_25 = resolve_food_composition("молоко 2.5%")
    res_milk_32 = resolve_food_composition("молоко 3.2%")

    assert res_milk_25.matched is True
    assert res_milk_32.matched is True
    assert res_milk_25.profile.canonical_id == "milk_2_5pct"
    assert res_milk_32.profile.canonical_id == "milk_3_2pct"
    assert res_milk_25.profile.fat_g == 2.5
    assert res_milk_32.profile.fat_g == 3.2
    assert res_milk_25.profile.calories_kcal == 52.0
    assert res_milk_32.profile.calories_kcal == 59.0
    assert res_milk_25.source_id == "SKURIKHIN-2008-0101"
    assert res_milk_32.source_id == "SKURIKHIN-2008-0102"


def test_cheese_types_distinct_profiles():
    """
    Requirement B.2: Specific cheese types have dedicated verified profiles
    and are not conflated with generic cream cheese or generic cheese.
    """
    # Mascarpone vs Cream Cheese
    res_mascarpone = resolve_food_composition("маскарпоне")
    res_cream_cheese = resolve_food_composition("сливочный сыр")

    assert res_mascarpone.matched is True
    assert res_mascarpone.profile.canonical_id == "mascarpone"
    assert res_mascarpone.profile.calories_kcal == 429.0
    assert res_mascarpone.profile.fat_g == 42.9
    assert res_mascarpone.source_id == "USDA-FDC-173440"
    assert res_mascarpone.profile.canonical_id != res_cream_cheese.profile.canonical_id

    # Parmesan
    res_parmesan = resolve_food_composition("пармезан")
    assert res_parmesan.matched is True
    assert res_parmesan.profile.canonical_id == "cheese_parmesan"
    assert res_parmesan.profile.protein_g == 35.8
    assert res_parmesan.source_id == "USDA-FDC-170845"

    # Cheddar
    res_cheddar = resolve_food_composition("чеддер")
    assert res_cheddar.matched is True
    assert res_cheddar.profile.canonical_id == "cheese_cheddar"
    assert res_cheddar.profile.fat_g == 33.1
    assert res_cheddar.source_id == "USDA-FDC-173418"

    # Gouda
    res_gouda = resolve_food_composition("гауда")
    assert res_gouda.matched is True
    assert res_gouda.profile.canonical_id == "cheese_gouda"
    assert res_gouda.source_id == "USDA-FDC-173426"

    # Mozzarella
    res_mozzarella = resolve_food_composition("моцарелла")
    assert res_mozzarella.matched is True
    assert res_mozzarella.profile.canonical_id == "cheese_mozzarella"
    assert res_mozzarella.source_id == "USDA-FDC-173420"

    # Feta
    res_feta = resolve_food_composition("фета")
    assert res_feta.matched is True
    assert res_feta.profile.canonical_id == "cheese_feta"
    assert res_feta.source_id == "USDA-FDC-173422"

    # Russian cheese
    res_russian = resolve_food_composition("сыр российский")
    assert res_russian.matched is True
    assert res_russian.profile.canonical_id == "cheese_russian"
    assert res_russian.source_id == "SKURIKHIN-2008-0120"


def test_cooking_states_distinct_profiles():
    """
    Requirement B.3: Distinct cooking states are separated and not conflated.
    """
    # Raw salmon vs baked salmon vs lightly salted salmon
    res_raw_salmon = resolve_food_composition("лосось")
    res_baked_salmon = resolve_food_composition("лосось запеченный")
    res_cured_salmon = resolve_food_composition("лосось слабосоленый")

    assert res_raw_salmon.matched is True
    assert res_baked_salmon.matched is True
    assert res_cured_salmon.matched is True

    assert res_raw_salmon.cooking_state == "raw"
    assert res_baked_salmon.cooking_state == "baked"
    assert res_cured_salmon.cooking_state == "cured"

    assert res_raw_salmon.profile.calories_kcal == 142.0
    assert res_baked_salmon.profile.calories_kcal == 206.0
    assert res_cured_salmon.profile.calories_kcal == 195.0

    # Raw mushrooms vs fried mushrooms
    res_raw_shamp = resolve_food_composition("шампиньоны")
    res_fried_shamp = resolve_food_composition("жареные грибы")

    assert res_raw_shamp.cooking_state == "raw"
    assert res_fried_shamp.cooking_state == "fried"
    assert res_raw_shamp.profile.fat_g == 1.0
    assert res_fried_shamp.profile.fat_g == 6.5

    # Boiled egg vs fried egg vs omelet
    res_boiled_egg = resolve_food_composition("вареное яйцо")
    res_fried_egg = resolve_food_composition("яичница")
    res_omelet = resolve_food_composition("омлет")

    assert res_boiled_egg.cooking_state == "boiled"
    assert res_fried_egg.cooking_state == "fried"
    assert res_omelet.cooking_state == "prepared"
    assert res_boiled_egg.profile.calories_kcal == 155.0
    assert res_fried_egg.profile.calories_kcal == 202.0
    assert res_omelet.profile.calories_kcal == 184.0

    # Raw beef vs boiled beef
    res_raw_beef = resolve_food_composition("говядина")
    res_boiled_beef = resolve_food_composition("отварная говядина")

    assert res_raw_beef.cooking_state == "raw"
    assert res_boiled_beef.cooking_state == "boiled"
    assert res_raw_beef.profile.calories_kcal == 187.0
    assert res_boiled_beef.profile.calories_kcal == 254.0


def test_catalog_source_traceability():
    """
    Requirement B.4: Every catalog entry has valid source_id, dataset_version,
    and cooking_state basis.
    """
    assert len(_FOOD_CATALOG) >= 45

    for profile in _FOOD_CATALOG:
        assert profile.source_id != "", f"Profile {profile.canonical_id} is missing source_id"
        assert profile.dataset_version != "", f"Profile {profile.canonical_id} is missing dataset_version"
        assert profile.cooking_state != "", f"Profile {profile.canonical_id} is missing cooking_state"
        assert profile.source_id.startswith("SKURIKHIN-") or profile.source_id.startswith("USDA-FDC-"), (
            f"Invalid source_id provenance {profile.source_id} for {profile.canonical_id}"
        )


def test_unknown_food_fails_closed_without_silent_substitution():
    """
    Requirement B.5: Where no defensible data exists, return an unresolved
    nutritional match. No silent substitution or fabricated values.
    """
    unknown_queries = [
        "жареная клубника",  # Cooking state mismatch for fruit
        "творог 18%",  # Non-standard unknown fat percentage
        "сыр с плесенью дорблю",  # Uncataloged cheese
        "экзотическая рыба фугу в карамели",  # Unknown dish
    ]

    for query in unknown_queries:
        res = resolve_food_composition(query)
        assert res.matched is False, f"Expected fail-closed (matched=False) for '{query}', got matched=True"
        assert res.profile is None


def test_incomplete_daily_totals_distinguishes_subtotal_and_no_target_achievement(tmp_path):
    """
    Requirement C: Incomplete diary totals explicitly distinguish known subtotal
    from complete total, and are NEVER presented as complete intake or full target achievement.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 901

    # Set user target to 2000 kcal
    _seed_targets(
        db_path,
        user_id=user_id,
        calories_kcal=2000.0,
        protein_g=100.0,
        fat_g=60.0,
        carbs_g=200.0,
    )

    # 1. Add complete meal exceeding target (>2000 kcal: 300g butter = 2244 kcal)
    inv_complete = FoodVisionInventory(
        schema_version="food_vision_inventory_v1",
        items=[
            FoodVisionItem(
                visible_name="Сливочное масло",
                normalized_name="сливочное масло",
                confidence=0.95,
                estimated_grams_min=295.0,
                estimated_grams_max=305.0,
            )
        ],
        overall_confidence=0.95,
        needs_user_confirmation=False,
        warnings=[],
    )
    diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=inv_complete,
        image_ref="ref:1",
        occurred_at=datetime.now(timezone.utc),
    )
    res_complete = diary.save_pending_inventory_directly(user_id)
    assert res_complete.saved is True

    # 2. Add an incomplete meal saved without KBZHU
    inv_incomplete = FoodVisionInventory(
        schema_version="food_vision_inventory_v1",
        items=[
            FoodVisionItem(
                visible_name="Экзотический салат",
                normalized_name="экзотический салат",
                confidence=0.95,
                estimated_grams_min=195.0,
                estimated_grams_max=205.0,
            )
        ],
        overall_confidence=0.95,
        needs_user_confirmation=False,
        warnings=[],
    )
    diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=inv_incomplete,
        image_ref="ref:2",
        occurred_at=datetime.now(timezone.utc),
    )
    res_incomplete = diary.save_pending_inventory_without_kbzhu(user_id)
    assert res_incomplete.saved is True

    # 3. Check daily summary structure
    summary = diary.get_daily_summary(user_id=user_id)
    assert summary["entry_count"] == 2
    assert summary["has_incomplete_entries"] is True
    assert summary["is_complete"] is False
    assert summary["incomplete_entry_count"] == 1
    assert summary["complete_entry_count"] == 1
    assert "known_subtotal" in summary
    assert summary["known_subtotal"]["calories_kcal"] > 2000.0

    # 4. Format report verification
    report = format_nutrition_diary_report(summary)

    # Must explicitly state partial subtotal
    assert "промежуточный итог" in report.lower()
    assert "учтено частично" in report.lower()
    # Must NOT present as full target achieved
    assert "Цель уже превышена" not in report
    # Progress line must show partial indicator (не менее X%)
    assert "не менее" in report


def test_one_tap_save_with_real_vision_inventory_estimated_weights(tmp_path):
    """
    Requirement D.1: One-tap save using actual FoodVisionInventory generated
    from Vision with proposed estimated weights.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 902

    # Simulate realistic FoodVisionInventory returned from Vision parser
    vision_inventory = FoodVisionInventory(
        schema_version="food_vision_inventory_v1",
        items=[
            FoodVisionItem(
                visible_name="Драники",
                normalized_name="драники",
                confidence=0.92,
                estimated_grams_min=190.0,
                estimated_grams_max=210.0,  # span 20g within auto-propose span (<=25g)
            ),
            FoodVisionItem(
                visible_name="Лосось слабосолёный",
                normalized_name="лосось слабосоленый",
                confidence=0.90,
                estimated_grams_min=90.0,
                estimated_grams_max=110.0,  # span 20g
            ),
        ],
        overall_confidence=0.91,
        needs_user_confirmation=False,
        warnings=[],
    )

    # Stage pending inventory from vision
    staged = diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=vision_inventory,
        image_ref="telegram:photo:902:1",
        occurred_at=datetime.now(timezone.utc),
    )
    assert staged.items[0].selected_grams == 200.0  # (190+210)/2
    assert staged.items[1].selected_grams == 100.0  # (90+110)/2

    # User executes One-Tap Save
    result = diary.handle_pending_inventory_reply(user_id, "✅ Сохранить")
    assert result.saved is True
    assert result.status == "saved"
    assert result.record is not None
    assert result.record.calories_kcal is not None
    assert result.record.calories_kcal > 0

    # Verify component provenance stored in DB
    summary = diary.get_daily_summary(user_id=user_id)
    assert summary["entry_count"] == 1
    items_json = json.loads(summary["entries"][0]["items_json"])
    assert len(items_json) == 2
    assert items_json[0]["source_id"] == "SKURIKHIN-2008-0714"
    assert items_json[0]["cooking_state"] == "fried"
    assert items_json[1]["source_id"] == "SKURIKHIN-2008-0322"
    assert items_json[1]["cooking_state"] == "cured"


def test_one_tap_save_with_real_vision_inventory_missing_weights(tmp_path):
    """
    Requirement D.2: One-tap save with missing weights prompts safely for weights
    before saving, and allows immediate save once weight is provided.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 903

    # Vision inventory with no estimated weight
    vision_inventory = FoodVisionInventory(
        schema_version="food_vision_inventory_v1",
        items=[
            FoodVisionItem(
                visible_name="Гречневая каша",
                normalized_name="гречневая каша",
                confidence=0.95,
                estimated_grams_min=None,
                estimated_grams_max=None,
            )
        ],
        overall_confidence=0.95,
        needs_user_confirmation=True,
        warnings=["Не удалось оценить вес по фото"],
    )

    # Stage inventory
    diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=vision_inventory,
        image_ref="telegram:photo:903:1",
        occurred_at=datetime.now(timezone.utc),
    )

    # 1. Attempt one-tap save without weight -> fails closed, prompts for weight
    res_attempt = diary.handle_pending_inventory_reply(user_id, "Сохранить")
    assert res_attempt.saved is False
    assert res_attempt.status == "needs_weight_confirmation"
    assert "укажите вес" in res_attempt.reply_text.lower()

    # 2. User specifies weight via valid command
    res_weight = diary.handle_pending_inventory_reply(user_id, "Вес 1: 250 г")
    assert res_weight.inventory.items[0].selected_grams == 250.0

    # 3. User taps Save again -> succeeds immediately
    res_saved = diary.handle_pending_inventory_reply(user_id, "Сохранить")
    assert res_saved.saved is True
    assert res_saved.status == "saved"
    assert res_saved.record.calories_kcal == pytest.approx(275.0, abs=1.0)  # 110 kcal * 2.5
