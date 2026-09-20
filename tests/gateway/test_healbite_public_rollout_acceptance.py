from __future__ import annotations

from pathlib import Path
import pytest

from gateway.healbite_family_telegram import HealBiteFamilyTelegramController
from gateway.healbite_households import HouseholdFeatureConfig
from gateway.healbite_weekly_menu_telegram import HealBiteWeeklyMenuTelegramController
from gateway.healbite_weekly_menu_runtime import build_weekly_menu_runtime_service
from gateway.healbite_shopping_telegram import HealBiteShoppingTelegramController
from gateway.healbite_shopping_runtime import build_shopping_runtime_service
from gateway.healbite_feature_gates import (
    FeatureAvailabilityStatus,
    FeatureGateConfig,
    evaluate_feature_gate,
    load_feature_gate_config,
)


def test_public_rollout_brand_new_user_can_access_all_core_features(tmp_path: Path) -> None:
    db_path = tmp_path / "acceptance_healbite.db"
    new_user_id = 888888
    existing_operator_id = 101

    # 1. Household Feature
    fam = HealBiteFamilyTelegramController(
        db_path=db_path,
        config=HouseholdFeatureConfig(
            enabled=True,
            allowlist=frozenset({existing_operator_id}),
            allowlist_valid=True,
            public_access=True,
        ),
    )
    fam_res = fam.home(new_user_id)
    assert fam_res.state != "disabled"
    assert fam_res.screen is not None

    # 2. Weekly Menu Feature
    menu = HealBiteWeeklyMenuTelegramController(
        db_path=db_path,
        runtime_factory=lambda: build_weekly_menu_runtime_service(
            db_path=db_path,
            env={
                "HEALBITE_WEEKLY_MENU_ENABLED": "true",
                "HEALBITE_WEEKLY_MENU_ALLOWLIST": str(existing_operator_id),
                "HEALBITE_WEEKLY_MENU_PUBLIC": "true",
            },
        ),
    )
    menu_res = menu.home(new_user_id)
    assert menu_res.state != "disabled"
    assert menu_res.screen is not None

    # 3. Shopping List Feature
    shop = HealBiteShoppingTelegramController(
        runtime_factory=lambda: build_shopping_runtime_service(
            db_path=db_path,
            env={
                "HEALBITE_SHOPPING_LIST_ENABLED": "true",
                "HEALBITE_SHOPPING_LIST_ALLOWLIST": str(existing_operator_id),
                "HEALBITE_SHOPPING_LIST_PUBLIC": "true",
            },
        ),
    )
    shop_res = shop.home(new_user_id)
    assert shop_res.state != "disabled"
    assert shop_res.screen is not None


def test_public_rollout_fails_closed_when_public_false(tmp_path: Path) -> None:
    db_path = tmp_path / "acceptance_healbite_closed.db"
    new_user_id = 888888
    existing_operator_id = 101

    fam = HealBiteFamilyTelegramController(
        db_path=db_path,
        config=HouseholdFeatureConfig(
            enabled=True,
            allowlist=frozenset({existing_operator_id}),
            allowlist_valid=True,
            public_access=False,
        ),
    )
    assert fam.home(new_user_id).state == "disabled"
    assert fam.home(existing_operator_id).state != "disabled"

    menu = HealBiteWeeklyMenuTelegramController(
        db_path=db_path,
        runtime_factory=lambda: build_weekly_menu_runtime_service(
            db_path=db_path,
            env={
                "HEALBITE_WEEKLY_MENU_ENABLED": "true",
                "HEALBITE_WEEKLY_MENU_ALLOWLIST": str(existing_operator_id),
                "HEALBITE_WEEKLY_MENU_PUBLIC": "false",
            },
        ),
    )
    assert menu.home(new_user_id).state == "disabled"
    assert menu.home(existing_operator_id).state != "disabled"

    shop = HealBiteShoppingTelegramController(
        runtime_factory=lambda: build_shopping_runtime_service(
            db_path=db_path,
            env={
                "HEALBITE_SHOPPING_LIST_ENABLED": "true",
                "HEALBITE_SHOPPING_LIST_ALLOWLIST": str(existing_operator_id),
                "HEALBITE_SHOPPING_LIST_PUBLIC": "false",
            },
        ),
    )
    assert shop.home(new_user_id).state == "disabled"
    assert shop.home(existing_operator_id).state != "disabled"


@pytest.mark.parametrize("bad_actor", [0, -1, None, "bad-actor", True, False])
def test_public_rollout_rejects_invalid_actors(tmp_path: Path, bad_actor: object) -> None:
    db_path = tmp_path / "acceptance_invalid_actors.db"

    fam = HealBiteFamilyTelegramController(
        db_path=db_path,
        config=HouseholdFeatureConfig(
            enabled=True,
            allowlist=frozenset({101}),
            allowlist_valid=True,
            public_access=True,
        ),
    )
    assert fam.home(bad_actor).state == "disabled"

    gate_cfg = FeatureGateConfig(
        enabled=True,
        allowlist=frozenset({101}),
        configuration_valid=True,
        public_access=True,
    )
    decision = evaluate_feature_gate(gate_cfg, bad_actor)
    assert decision.status is FeatureAvailabilityStatus.INVALID_ACTOR
    assert decision.ready is False


def test_public_rollout_user_isolation(tmp_path: Path) -> None:
    db_path = tmp_path / "acceptance_isolation.db"
    user_a = 700001
    user_b = 700002

    fam = HealBiteFamilyTelegramController(
        db_path=db_path,
        config=HouseholdFeatureConfig(
            enabled=True,
            allowlist=frozenset({101}),
            allowlist_valid=True,
            public_access=True,
        ),
    )
    res_a = fam.home(user_a)
    res_b = fam.home(user_b)
    assert res_a.state != "disabled"
    assert res_b.state != "disabled"
