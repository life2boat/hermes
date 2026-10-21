import hashlib
import json
import os
from pathlib import Path
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

FIXTURE_PATH = Path(__file__).resolve().parent.parent.parent / "gateway" / "fixtures" / "food_reference_catalog_v1.json"
EXPECTED_FIXTURE_SHA256 = "a582a36822022c3c35ec61e52b147a4202da4b3d1aa35cae66e22ad7c24fd8e3"


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


def test_catalog_fixture_hash_and_value_verification():
    """
    Requirement 7: Deterministic catalog-wide validation against a version-pinned,
    hash-verified source fixture. Tests verify source identity AND nutrient values,
    not just nonempty metadata.
    """
    assert FIXTURE_PATH.is_file(), f"Missing fixture at {FIXTURE_PATH}"
    fixture_content = FIXTURE_PATH.read_text(encoding="utf-8")
    actual_sha256 = hashlib.sha256(fixture_content.encode("utf-8")).hexdigest()
    assert actual_sha256 == EXPECTED_FIXTURE_SHA256, (
        f"Fixture SHA-256 drift! Expected {EXPECTED_FIXTURE_SHA256}, got {actual_sha256}"
    )

    fixture_items = json.loads(fixture_content)
    fixture_by_id = {item["canonical_id"]: item for item in fixture_items}
    assert len(fixture_by_id) == len(_FOOD_CATALOG), (
        f"Catalog size ({len(_FOOD_CATALOG)}) does not match fixture size ({len(fixture_by_id)})"
    )

    usda_count = 0
    russian_count = 0

    for profile in _FOOD_CATALOG:
        assert profile.canonical_id in fixture_by_id, f"Profile {profile.canonical_id} missing from fixture"
        fix = fixture_by_id[profile.canonical_id]

        # Verify source provenance and metadata
        assert profile.source_id == fix["source_id"]
        assert profile.dataset_version == fix["dataset_version"]
        assert profile.cooking_state == fix["cooking_state"]
        assert profile.source_provenance == fix["source_provenance"]
        assert profile.source_provenance != "", f"Empty source_provenance for {profile.canonical_id}"

        # Verify exact nutrient values
        assert profile.calories_kcal == pytest.approx(fix["calories_kcal"], abs=0.01)
        assert profile.protein_g == pytest.approx(fix["protein_g"], abs=0.01)
        assert profile.fat_g == pytest.approx(fix["fat_g"], abs=0.01)
        assert profile.carbs_g == pytest.approx(fix["carbs_g"], abs=0.01)

        if profile.source_id.startswith("USDA-FDC-"):
            usda_count += 1
            assert "USDA FoodData Central" in profile.source_provenance
        elif profile.source_id.startswith("INTERNAL-SKURIKHIN-"):
            russian_count += 1
            assert "Скурихин" in profile.source_provenance
        else:
            pytest.fail(f"Invalid source_id prefix: {profile.source_id}")

    assert usda_count == 18, f"Expected 18 USDA verified entries, got {usda_count}"
    assert russian_count == 52, f"Expected 52 Russian reference entries, got {russian_count}"


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
    assert res_tvorog_5.source_id == "INTERNAL-SKURIKHIN-2008-0105"
    assert res_tvorog_9.source_id == "INTERNAL-SKURIKHIN-2008-0104"

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
    assert res_smetana_15.source_id == "INTERNAL-SKURIKHIN-2008-0112"
    assert res_smetana_20.source_id == "INTERNAL-SKURIKHIN-2008-0113"

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
    assert res_milk_25.source_id == "INTERNAL-SKURIKHIN-2008-0101"
    assert res_milk_32.source_id == "INTERNAL-SKURIKHIN-2008-0102"


def test_cheese_types_distinct_profiles():
    """
    Requirement B.2: Specific cheese types have dedicated verified profiles.
    Unverified cheeses (mascarpone) fail closed (matched=False).
    Verified cheeses match actual USDA FDC records.
    """
    # Mascarpone: unverified in reference editions -> fails closed
    res_mascarpone = resolve_food_composition("маскарпоне")
    assert res_mascarpone.matched is False
    assert res_mascarpone.profile is None

    # Cream Cheese (FDC 173418)
    res_cream_cheese = resolve_food_composition("сливочный сыр")
    assert res_cream_cheese.matched is True
    assert res_cream_cheese.profile.canonical_id == "cream_cheese"
    assert res_cream_cheese.source_id == "USDA-FDC-173418"
    assert res_cream_cheese.profile.calories_kcal == 350.0
    assert res_cream_cheese.profile.fat_g == 34.4
    assert res_cream_cheese.profile.protein_g == 6.2
    assert res_cream_cheese.profile.carbs_g == 5.5

    # Parmesan (FDC 170848)
    res_parmesan = resolve_food_composition("пармезан")
    assert res_parmesan.matched is True
    assert res_parmesan.profile.canonical_id == "cheese_parmesan"
    assert res_parmesan.source_id == "USDA-FDC-170848"
    assert res_parmesan.profile.calories_kcal == 392.0
    assert res_parmesan.profile.protein_g == 35.8
    assert res_parmesan.profile.fat_g == 25.0

    # Cheddar (FDC 173414)
    res_cheddar = resolve_food_composition("чеддер")
    assert res_cheddar.matched is True
    assert res_cheddar.profile.canonical_id == "cheese_cheddar"
    assert res_cheddar.source_id == "USDA-FDC-173414"
    assert res_cheddar.profile.calories_kcal == 403.0
    assert res_cheddar.profile.fat_g == 33.3

    # Gouda (FDC 171241)
    res_gouda = resolve_food_composition("гауда")
    assert res_gouda.matched is True
    assert res_gouda.profile.canonical_id == "cheese_gouda"
    assert res_gouda.source_id == "USDA-FDC-171241"
    assert res_gouda.profile.calories_kcal == 356.0
    assert res_gouda.profile.fat_g == 27.4

    # Mozzarella (FDC 170845)
    res_mozzarella = resolve_food_composition("моцарелла")
    assert res_mozzarella.matched is True
    assert res_mozzarella.profile.canonical_id == "cheese_mozzarella"
    assert res_mozzarella.source_id == "USDA-FDC-170845"
    assert res_mozzarella.profile.calories_kcal == 299.0
    assert res_mozzarella.profile.protein_g == 22.2

    # Feta (FDC 173420)
    res_feta = resolve_food_composition("фета")
    assert res_feta.matched is True
    assert res_feta.profile.canonical_id == "cheese_feta"
    assert res_feta.source_id == "USDA-FDC-173420"
    assert res_feta.profile.calories_kcal == 265.0
    assert res_feta.profile.protein_g == 14.2
    assert res_feta.profile.fat_g == 21.5

    # Brie (FDC 172177)
    res_brie = resolve_food_composition("бри")
    assert res_brie.matched is True
    assert res_brie.profile.canonical_id == "cheese_brie"
    assert res_brie.source_id == "USDA-FDC-172177"
    assert res_brie.profile.calories_kcal == 334.0

    # Edam (FDC 173419)
    res_edam = resolve_food_composition("эдам")
    assert res_edam.matched is True
    assert res_edam.profile.canonical_id == "cheese_edam"
    assert res_edam.source_id == "USDA-FDC-173419"
    assert res_edam.profile.calories_kcal == 357.0

    # Swiss (FDC 171251)
    res_swiss = resolve_food_composition("швейцарский сыр")
    assert res_swiss.matched is True
    assert res_swiss.profile.canonical_id == "cheese_swiss"
    assert res_swiss.source_id == "USDA-FDC-171251"
    assert res_swiss.profile.calories_kcal == 393.0

    # Russian cheese
    res_russian = resolve_food_composition("сыр российский")
    assert res_russian.matched is True
    assert res_russian.profile.canonical_id == "cheese_russian"
    assert res_russian.source_id == "INTERNAL-SKURIKHIN-2008-0120"
    assert res_russian.profile.calories_kcal == 364.0


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

    assert res_raw_salmon.source_id == "USDA-FDC-173686"
    assert res_baked_salmon.source_id == "USDA-FDC-171998"
    assert res_cured_salmon.source_id == "INTERNAL-SKURIKHIN-2008-0322"

    assert res_raw_salmon.profile.calories_kcal == 142.0
    assert res_baked_salmon.profile.calories_kcal == 182.0
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
        "маскарпоне",  # Unverified cheese left unresolved
        "чечевичный суп с овощами",  # Composite unverified soup
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
    assert items_json[0]["source_id"] == "INTERNAL-SKURIKHIN-2008-0714"
    assert items_json[0]["cooking_state"] == "fried"
    assert items_json[0]["source_provenance"] != ""
    assert items_json[1]["source_id"] == "INTERNAL-SKURIKHIN-2008-0322"
    assert items_json[1]["cooking_state"] == "cured"
    assert items_json[1]["source_provenance"] != ""


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
