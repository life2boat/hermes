from __future__ import annotations

import json
import logging
import sqlite3
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent.auxiliary_client import LLMServiceUnavailableError
from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_inventory import (
    HealBiteInventoryStore,
    InventoryItemInput,
    InventoryOwnerScope,
)
from gateway.healbite_inventory_menu_contract import INVENTORY_MENU_RESPONSE_CONTRACT
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.healbite_weekly_menu_generation import (
    AuxiliaryWeeklyMenuGenerator,
    CanonicalWeeklyMenuMemberSnapshotProvider,
    HealBiteWeeklyMenuGenerationService,
    WeeklyMenuGenerationStatus,
    WeeklyMenuGeneratorUnavailableError,
    WeeklyMenuGeneratorValidationError,
)
from gateway.healbite_weekly_menu_generation_types import (
    WeeklyMenuGenerationRequest,
    WeeklyMenuInventoryItem,
)


LOGGER_NAME = "gateway.healbite_weekly_menu_generation"
WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
MEAL_SLOTS = ("breakfast", "lunch", "dinner")


def _request() -> WeeklyMenuGenerationRequest:
    return WeeklyMenuGenerationRequest(
        week_start="2026-07-06",
        dates=(
            "2026-07-06",
            "2026-07-07",
            "2026-07-08",
            "2026-07-09",
            "2026-07-10",
            "2026-07-11",
            "2026-07-12",
        ),
        allowed_meal_slots=MEAL_SLOTS,
        locale="ru-RU",
        member_count=1,
        members=(),
        household_dietary_notes=(),
        max_entries=21,
        inventory_snapshot_id="synthetic-snapshot",
        inventory_items=(
            WeeklyMenuInventoryItem(
                normalized_name="synthetic ingredient",
                display_name="synthetic ingredient",
                quantity_value="25000",
                unit="g",
                category=None,
            ),
        ),
        inventory_only=True,
    )


def _valid_payload() -> dict[str, object]:
    return {
        "days": [
            {
                "day": day,
                "meals": [
                    {
                        "meal_type": slot,
                        "title": f"synthetic {day} {slot}",
                        "instructions": ["synthetic preparation step"],
                        "servings": 2,
                        "estimated_calories_per_serving": 500,
                        "macros_per_serving": {
                            "protein_g": 30,
                            "carbs_g": 40,
                            "fat_g": 15,
                        },
                        "ingredients": [
                            {
                                "name": "synthetic ingredient",
                                "quantity_value": "500",
                                "unit": "g",
                            }
                        ],
                    }
                    for slot in MEAL_SLOTS
                ],
            }
            for day in WEEKDAYS
        ]
    }


def _response(content: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _call_with_content(content: str | None, capture: dict[str, object] | None = None):
    def _call(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        telemetry = kwargs["request_telemetry"]
        telemetry.external_request_budget = kwargs["call_policy"].max_external_requests
        telemetry.external_request_attempts += 1
        return _response(content)

    return _call


def _generate_content(content: str | None, caplog) -> None:
    generator = AuxiliaryWeeklyMenuGenerator(call_llm_fn=_call_with_content(content))
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        generator.generate(_request())


def test_valid_seven_day_inventory_response_passes_with_success_telemetry(caplog):
    payload = _valid_payload()
    payload["days"][0]["meals"][0]["title"] = "SENSITIVE_MENU_TEXT"
    generator = AuxiliaryWeeklyMenuGenerator(
        call_llm_fn=_call_with_content(json.dumps(payload))
    )

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        result = generator.generate(_request())

    assert len(result.entries) == 21
    assert "outcome=success" in caplog.text
    assert "safe_reason=NONE" in caplog.text
    assert "provider_failure" not in caplog.text
    assert "SENSITIVE_MENU_TEXT" not in caplog.text


def test_inventory_prompt_embeds_the_exact_validator_contract():
    capture: dict[str, object] = {}
    generator = AuxiliaryWeeklyMenuGenerator(
        call_llm_fn=_call_with_content(json.dumps(_valid_payload()), capture)
    )

    generator.generate(_request())

    messages = capture["messages"]
    assert [message["role"] for message in messages] == ["system", "user"]
    system_prompt = messages[0]["content"]
    serialized_contract = json.dumps(
        INVENTORY_MENU_RESPONSE_CONTRACT,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert serialized_contract in system_prompt
    assert INVENTORY_MENU_RESPONSE_CONTRACT["top_level"]["required_keys"] == ["days"]
    assert INVENTORY_MENU_RESPONSE_CONTRACT["days"]["length"] == 7
    assert INVENTORY_MENU_RESPONSE_CONTRACT["days"]["required_keys"] == [
        "day",
        "meals",
    ]
    assert INVENTORY_MENU_RESPONSE_CONTRACT["meal"]["meal_type_values"] == list(
        MEAL_SLOTS
    )
    assert set(INVENTORY_MENU_RESPONSE_CONTRACT["meal"]["required_keys"]) == {
        "meal_type",
        "title",
        "instructions",
        "servings",
        "estimated_calories_per_serving",
        "macros_per_serving",
        "ingredients",
    }
    assert set(
        INVENTORY_MENU_RESPONSE_CONTRACT["macros_per_serving"]["required_keys"]
    ) == {"protein_g", "carbs_g", "fat_g"}
    assert set(INVENTORY_MENU_RESPONSE_CONTRACT["ingredient"]["required_keys"]) == {
        "name",
        "quantity_value",
        "unit",
    }
    assert all(
        section.get("additional_keys") is False
        for section in (
            INVENTORY_MENU_RESPONSE_CONTRACT["top_level"],
            INVENTORY_MENU_RESPONSE_CONTRACT["days"],
            INVENTORY_MENU_RESPONSE_CONTRACT["meal"],
            INVENTORY_MENU_RESPONSE_CONTRACT["macros_per_serving"],
            INVENTORY_MENU_RESPONSE_CONTRACT["ingredient"],
        )
    )


def _invalid_payload(case: str) -> tuple[dict[str, object], str]:
    payload = deepcopy(_valid_payload())
    if case == "missing_top_level":
        return {}, "INVALID_TOP_LEVEL_SHAPE"
    if case == "wrong_day_count":
        payload["days"].pop()
        return payload, "INVALID_DAY_COUNT_OR_TYPE"
    if case == "invalid_day_structure":
        payload["days"][0]["extra"] = True
        return payload, "INVALID_DAY_STRUCTURE"
    if case == "missing_meal_slot":
        payload["days"][0]["meals"].pop()
        return payload, "MISSING_OR_INVALID_MEAL_SLOTS"
    if case == "wrong_field_type":
        payload["days"][0]["meals"][0]["servings"] = False
        return payload, "INVALID_SERVINGS"
    if case == "invalid_day":
        payload["days"][0]["day"] = "nonday"
        return payload, "INVALID_DAY_VALUE"
    if case == "extra_meal_key":
        payload["days"][0]["meals"][0]["extra"] = True
        return payload, "INVALID_MEAL_STRUCTURE"
    raise AssertionError(f"unknown synthetic case: {case}")


@pytest.mark.parametrize(
    "case",
    [
        "missing_top_level",
        "wrong_day_count",
        "invalid_day_structure",
        "missing_meal_slot",
        "wrong_field_type",
        "invalid_day",
        "extra_meal_key",
    ],
)
def test_inventory_schema_failures_are_normalized_and_not_provider_failures(
    case, caplog
):
    payload, expected_reason = _invalid_payload(case)
    generator = AuxiliaryWeeklyMenuGenerator(
        call_llm_fn=_call_with_content(json.dumps(payload))
    )

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(WeeklyMenuGeneratorValidationError):
            generator.generate(_request())

    assert "outcome=schema_validation_failure" in caplog.text
    assert f"safe_reason={expected_reason}" in caplog.text
    assert "outcome=provider_failure" not in caplog.text


@pytest.mark.parametrize(
    ("content", "outcome", "reason"),
    [
        (
            "provider prose " + json.dumps(_valid_payload()),
            "json_parse_failure",
            "MALFORMED_JSON",
        ),
        ("{not-json", "json_parse_failure", "MALFORMED_JSON"),
        (None, "empty_provider_response", "EMPTY_PROVIDER_RESPONSE"),
    ],
)
def test_non_json_and_empty_provider_responses_fail_closed(
    content, outcome, reason, caplog
):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(WeeklyMenuGeneratorValidationError):
            _generate_content(content, caplog)

    assert f"outcome={outcome}" in caplog.text
    assert f"safe_reason={reason}" in caplog.text
    assert "outcome=success" not in caplog.text


class _SyntheticHttpError(Exception):
    status_code = 400


class _SyntheticTimeoutError(Exception):
    pass


@pytest.mark.parametrize(
    ("cause", "outcome", "reason"),
    [
        (_SyntheticHttpError("DO_NOT_LOG_HTTP_BODY"), "http_rejection", "HTTP_4XX"),
        (
            _SyntheticTimeoutError("DO_NOT_LOG_TRANSPORT_DETAIL"),
            "transport_failure",
            "TRANSPORT_ERROR",
        ),
    ],
)
def test_provider_failures_remain_distinct_and_safe(cause, outcome, reason, caplog):
    def _call(**kwargs):
        telemetry = kwargs["request_telemetry"]
        telemetry.external_request_budget = kwargs["call_policy"].max_external_requests
        telemetry.external_request_attempts += 1
        raise LLMServiceUnavailableError(
            "DO_NOT_LOG_WRAPPER_DETAIL",
            task="weekly_menu_generation",
            cause=cause,
        )

    generator = AuxiliaryWeeklyMenuGenerator(call_llm_fn=_call)

    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        with pytest.raises(WeeklyMenuGeneratorUnavailableError):
            generator.generate(_request())

    assert f"outcome={outcome}" in caplog.text
    assert f"safe_reason={reason}" in caplog.text
    assert "schema_validation_failure" not in caplog.text
    assert "DO_NOT_LOG" not in caplog.text


def _table_counts(connection: sqlite3.Connection, prefix: str) -> dict[str, int]:
    names = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?",
            (f"{prefix}%",),
        )
    ]
    return {
        name: int(connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
        for name in names
    }


def test_inventory_contract_failure_creates_no_draft_or_domain_mutations(tmp_path):
    db_path = tmp_path / "contract-failure.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT, created_at TEXT)"
        )
