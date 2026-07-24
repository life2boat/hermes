from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_inventory import (
    HealBiteInventoryStore,
    InventoryOwnerScope,
    InventoryStatus,
)
from gateway.healbite_inventory_telegram import HealBiteInventoryTelegramController


ACTOR = 8_000_000_000_000_002_101


def _gate() -> FeatureGateConfig:
    return FeatureGateConfig(enabled=True, allowlist=frozenset({ACTOR}))


def _seed_household(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(user_id INTEGER PRIMARY KEY, username TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO users "
            "(user_id, username, created_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (ACTOR, "synthetic"),
        )
    return HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(
        ACTOR
    )


def _controller(db_path: Path) -> HealBiteInventoryTelegramController:
    return HealBiteInventoryTelegramController(
        text_config=_gate(),
        photo_config=_gate(),
        weekly_generation_config=_gate(),
        db_path=db_path,
    )


def _find_callback(result, label_fragment: str) -> str:
    for row in result.screen.rows:
        for label, callback_data in row:
            if label_fragment in label:
                return callback_data
    raise AssertionError(f"missing callback: {label_fragment}")


def _aux_response() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "items": [
                                {
                                    "name": "milk",
                                    "quantity_value": "1",
                                    "unit": "l",
                                    "uncertain": True,
                                }
                            ]
                        }
                    )
                )
            )
        ]
    )


@pytest.mark.asyncio
async def test_default_vision_uses_strict_auxiliary_route_not_direct_tool(
    tmp_path, monkeypatch
):
    observed = {}

    async def canonical_auxiliary(**kwargs):
        observed.update(kwargs)
        return _aux_response()

    from tools import vision_tools

    direct_tool = AsyncMock(side_effect=AssertionError("direct tool route used"))
    monkeypatch.setattr(vision_tools, "vision_analyze_tool", direct_tool)
    monkeypatch.setattr(
        "agent.auxiliary_client.async_call_llm", canonical_auxiliary
    )
    image_path = tmp_path / "input.jpg"
    image_path.write_bytes(b"synthetic-image")

    result = await _controller(tmp_path / "route.db")._default_vision_analyze(
        str(image_path), "visible items only"
    )

    assert result == {"success": True, "analysis": _aux_response().choices[0].message.content}
    assert observed["task"] == "vision"
    policy = observed["call_policy"]
    assert policy.max_external_requests == 1
    assert policy.retry_transient is False
    assert policy.retry_without_temperature is False
    assert policy.retry_without_max_tokens is False
    assert policy.refresh_model is False
    assert policy.recover_credentials is False
    assert policy.fallback_provider is False
    assert policy.fallback_model is False
    image_block = observed["messages"][0]["content"][1]
    assert image_block["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert str(image_path) not in image_block["image_url"]["url"]
    direct_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_auxiliary_failure_is_masked_and_photo_flow_fails_closed(
    tmp_path, monkeypatch, caplog
):
    async def unavailable(**_kwargs):
        raise RuntimeError("raw provider credential failure")

    monkeypatch.setattr("agent.auxiliary_client.async_call_llm", unavailable)
    db_path = tmp_path / "failure.db"
    _seed_household(db_path)
    controller = _controller(db_path)
    controller.handle_callback(ACTOR, _find_callback(controller.home(ACTOR), "фотографию"))

    with caplog.at_level(logging.INFO):
        result = await controller.handle_photo_bytes(ACTOR, b"private-image-id")

    assert result is not None and result.state == "vision_unavailable"
    assert "credential" not in result.screen.text.lower()
    assert "private-image-id" not in caplog.text
    assert "raw provider credential failure" not in caplog.text
    assert controller.pending_input_kind(ACTOR) == "text"


@pytest.mark.asyncio
async def test_auxiliary_photo_result_stays_pending_without_generation_or_shopping(
    tmp_path, monkeypatch
):
    async def canonical_auxiliary(**_kwargs):
        return _aux_response()

    monkeypatch.setattr(
        "agent.auxiliary_client.async_call_llm", canonical_auxiliary
    )
    db_path = tmp_path / "pending.db"
    household = _seed_household(db_path)
    controller = _controller(db_path)
    controller.handle_callback(ACTOR, _find_callback(controller.home(ACTOR), "фотографию"))

    review = await controller.handle_photo_bytes(ACTOR, b"synthetic-image")

    assert review is not None and review.state == "review"
    confirmation = _find_callback(review, "Подтвердить")
    snapshot_id = confirmation.split(":")[3]
    view = HealBiteInventoryStore(db_path=db_path).get_snapshot(
        InventoryOwnerScope(household_id=household.household.id), snapshot_id
    )
    assert view.snapshot.status is InventoryStatus.PENDING
    with sqlite3.connect(db_path) as conn:
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert not any("shopping" in name for name in table_names)
