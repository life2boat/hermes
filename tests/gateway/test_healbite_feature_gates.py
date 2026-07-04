from __future__ import annotations

from pathlib import Path

from gateway.healbite_feature_gates import (
    FeatureAvailabilityStatus,
    FeatureGateConfig,
    evaluate_feature_gate,
    load_feature_gate_config,
    normalize_actor_user_id,
)


def test_load_feature_gate_config_defaults_disabled_and_empty_allowlist():
    config = load_feature_gate_config("HEALBITE_WEEKLY_MENU", {})

    assert config == FeatureGateConfig(enabled=False, allowlist=frozenset(), configuration_valid=True)


def test_load_feature_gate_config_accepts_boolean_and_numeric_allowlist():
    config = load_feature_gate_config(
        "HEALBITE_WEEKLY_MENU",
        {
            "HEALBITE_WEEKLY_MENU_ENABLED": "true",
            "HEALBITE_WEEKLY_MENU_ALLOWLIST": "101, 202;202",
        },
    )

    assert config.enabled is True
    assert config.allowlist == frozenset({101, 202})
    assert config.configuration_valid is True


def test_load_feature_gate_config_fails_closed_for_malformed_boolean():
    config = load_feature_gate_config(
        "HEALBITE_WEEKLY_MENU",
        {
            "HEALBITE_WEEKLY_MENU_ENABLED": "maybe",
            "HEALBITE_WEEKLY_MENU_ALLOWLIST": "101",
        },
    )

    assert config.enabled is False
    assert config.allowlist == frozenset()
    assert config.configuration_valid is False


def test_load_feature_gate_config_fail_closed_for_malformed_allowlist():
    config = load_feature_gate_config(
        "HEALBITE_SHOPPING_LIST",
        {
            "HEALBITE_SHOPPING_LIST_ENABLED": "1",
            "HEALBITE_SHOPPING_LIST_ALLOWLIST": "101,not-a-number",
        },
    )

    assert config.enabled is False
    assert config.allowlist == frozenset()
    assert config.configuration_valid is False


def test_evaluate_feature_gate_orders_disabled_before_actor_validation():
    decision = evaluate_feature_gate(
        FeatureGateConfig(enabled=False, allowlist=frozenset({101}), configuration_valid=True),
        "not-an-int",
    )

    assert decision.status is FeatureAvailabilityStatus.DISABLED


def test_evaluate_feature_gate_returns_invalid_actor_for_bad_identity_when_enabled():
    decision = evaluate_feature_gate(
        FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        "not-an-int",
    )

    assert decision.status is FeatureAvailabilityStatus.INVALID_ACTOR


def test_evaluate_feature_gate_returns_not_allowlisted_with_empty_allowlist():
    decision = evaluate_feature_gate(
        FeatureGateConfig(enabled=True, allowlist=frozenset(), configuration_valid=True),
        101,
    )

    assert decision.status is FeatureAvailabilityStatus.NOT_ALLOWLISTED


def test_evaluate_feature_gate_returns_ready_for_allowlisted_actor():
    decision = evaluate_feature_gate(
        FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        "101",
    )

    assert decision.status is FeatureAvailabilityStatus.READY
    assert decision.actor_user_id == 101
    assert decision.ready is True


def test_normalize_actor_user_id_rejects_bool_zero_and_oversized():
    assert normalize_actor_user_id(True) is None
    assert normalize_actor_user_id(0) is None
    assert normalize_actor_user_id(-1) is None
    assert normalize_actor_user_id(9223372036854775808) is None



def test_runtime_modules_import_without_side_effects(tmp_path):
    import os
    import subprocess
    import sys

    db_path = tmp_path / "import-only.db"
    env = os.environ.copy()
    env["HEALBITE_DB_PATH"] = str(db_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import gateway.healbite_feature_gates\n"
                "import gateway.healbite_weekly_menu_runtime\n"
                "import gateway.healbite_shopping_runtime\n"
                "print('OK')\n"
            ),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(Path(__file__).resolve().parents[2]),
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
    assert not db_path.exists()
