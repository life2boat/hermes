"""Regression test suite for HEALBITE_ADMIN_AUTHORIZATION_HARDENING_V1.

Verifies fail-closed administrative authorization invariants:
1. Empty or missing admin allowlist denies administrative privileges.
2. Invalid configurations fail closed safely.
3. The owner retains admin privileges under valid configuration.
4. Target ID 248875361 is denied admin in both DM and group scopes.
5. DM and Group admin allowlists remain strictly isolated.
6. Public commands and onboarding remain reachable.
7. User nutrition diary, profile, and ordinary commands remain accessible.
8. No alternative bypasses (running agent or direct dispatch) allow admin execution without proof.
9. Malformed types and unexpected config values are handled safely.
10. Authorization parity with all existing contracts.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource
from gateway.slash_access import (
    ADMIN_COMMANDS,
    PUBLIC_USER_COMMANDS,
    SlashAccessPolicy,
    policy_for_source,
    policy_from_extra,
)


def _make_source(
    *,
    platform: Platform = Platform.TELEGRAM,
    user_id: str = "synthetic_user_999",
    chat_type: str = "dm",
    chat_id: str = "1001",
) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name=f"name-{user_id}",
        chat_type=chat_type,
    )


def _make_event(text: str, source: SessionSource) -> MessageEvent:
    return MessageEvent(text=text, source=source, message_id="msg_test_1")


def _make_runner(*, platform_extra: dict | None = None, platform: Platform = Platform.TELEGRAM):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            platform: PlatformConfig(
                enabled=True,
                token="synthetic_token",
                extra=platform_extra or {},
            )
        }
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {platform: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    session_entry = SessionEntry(
        session_key=f"agent:main:{platform.value}:dm:1001",
        session_id="sess_test_1",
        created_at=None,
        updated_at=None,
        platform=platform,
        chat_type="dm",
        total_tokens=0,
    )
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_run_generation = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_sources = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._session_db.get_session.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


# ---------------------------------------------------------------------------
# 1. Empty or missing admin allowlist denies arbitrary user administrative powers
# ---------------------------------------------------------------------------


def test_empty_or_missing_admin_allowlist_denies_admin_privileges():
    policy = policy_from_extra({}, "dm")
    assert policy.enabled is False
    assert policy.admin_user_ids == frozenset()

    # is_admin must never return True without positive proof
    assert policy.is_admin("synthetic_user_999") is False
    assert policy.is_admin(None) is False
    assert policy.is_admin("") is False

    # Administrative commands fail closed
    for admin_cmd in ("restart", "stop", "config", "reload", "yolo", "model", "memory_stats", "sethome"):
        assert policy.can_run("synthetic_user_999", admin_cmd) is False

    # Public commands stay accessible
    for public_cmd in ("help", "whoami", "diary", "profile", "weight", "water", "start", "menu"):
        assert policy.can_run("synthetic_user_999", public_cmd) is True


@pytest.mark.asyncio
async def test_empty_admin_allowlist_dispatches_denial_for_admin_commands():
    runner = _make_runner(platform_extra={})
    source = _make_source(user_id="synthetic_user_999")

    result = await runner._handle_message(_make_event("/restart", source))
    assert result is not None
    assert "⛔" in result
    assert "/restart is admin-only here" in result


# ---------------------------------------------------------------------------
# 2. Invalid configuration does not fail open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "invalid_extra",
    [
        {"allow_admin_from": True},
        {"allow_admin_from": False},
        {"allow_admin_from": {}},
        {"allow_admin_from": "*"},
        {"allow_admin_from": [True, False, None, {}, "*", "   "]},
        {"allow_admin_from": ""},
    ],
)
def test_invalid_config_does_not_fail_open(invalid_extra):
    policy = policy_from_extra(invalid_extra, "dm")
    assert policy.enabled is False
    assert policy.is_admin("synthetic_user_999") is False
    assert policy.is_admin("True") is False
    assert policy.is_admin("*") is False
    assert policy.can_run("synthetic_user_999", "restart") is False
    assert policy.can_run("synthetic_user_999", "stop") is False
    # Public commands remain operational
    assert policy.can_run("synthetic_user_999", "help") is True
    assert policy.can_run("synthetic_user_999", "diary") is True


# ---------------------------------------------------------------------------
# 3. Owner receives administrative access with valid configuration
# ---------------------------------------------------------------------------


def test_owner_retains_admin_access_with_valid_config():
    owner_id = "968323641"
    policy = policy_from_extra({"allow_admin_from": [owner_id]}, "dm")
    assert policy.enabled is True
    assert policy.is_admin(owner_id) is True
    assert policy.can_run(owner_id, "restart") is True
    assert policy.can_run(owner_id, "status") is True
    assert policy.can_run(owner_id, "yolo") is True
    assert policy.can_run(owner_id, "diary") is True


@pytest.mark.asyncio
async def test_owner_dispatches_admin_command_without_denial():
    owner_id = "968323641"
    runner = _make_runner(platform_extra={"allow_admin_from": [owner_id]})
    source = _make_source(user_id=owner_id)

    result = await runner._handle_message(_make_event("/whoami", source))
    assert result is not None
    assert "⛔" not in result
    assert "**admin**" in result


# ---------------------------------------------------------------------------
# 4. Telegram ID 248875361 denied administrative access in DM and Groups
# ---------------------------------------------------------------------------


def test_target_248875361_denied_admin_in_both_dm_and_groups():
    target_id = "248875361"
    owner_id = "968323641"
    extra = {
        "allow_admin_from": [owner_id],
        "group_allow_admin_from": [owner_id],
    }

    dm_policy = policy_from_extra(extra, "dm")
    group_policy = policy_from_extra(extra, "group")

    # In DM
    assert dm_policy.is_admin(target_id) is False
    assert dm_policy.can_run(target_id, "status") is False
    assert dm_policy.can_run(target_id, "restart") is False
    assert dm_policy.can_run(target_id, "yolo") is False

    # In Group
    assert group_policy.is_admin(target_id) is False
    assert group_policy.can_run(target_id, "status") is False
    assert group_policy.can_run(target_id, "restart") is False
    assert group_policy.can_run(target_id, "yolo") is False

    # Target user can still run allowed user commands
    assert dm_policy.can_run(target_id, "diary") is True
    assert dm_policy.can_run(target_id, "profile") is True


@pytest.mark.asyncio
async def test_target_248875361_dispatch_denied_admin_commands():
    target_id = "248875361"
    runner = _make_runner(
        platform_extra={
            "allow_admin_from": ["968323641"],
            "group_allow_admin_from": ["968323641"],
        }
    )

    dm_source = _make_source(user_id=target_id, chat_type="dm")
    grp_source = _make_source(user_id=target_id, chat_type="group")

    dm_res = await runner._handle_message(_make_event("/status", dm_source))
    assert "⛔" in dm_res
    assert "/status is admin-only here" in dm_res

    grp_res = await runner._handle_message(_make_event("/status", grp_source))
    assert "⛔" in grp_res
    assert "/status is admin-only here" in grp_res


# ---------------------------------------------------------------------------
# 5. DM-allowlist does not automatically grant group admin and vice versa
# ---------------------------------------------------------------------------


def test_dm_and_group_allowlist_isolation():
    dm_admin = "1110001"
    group_admin = "2220002"
    extra = {
        "allow_admin_from": [dm_admin],
        "group_allow_admin_from": [group_admin],
    }

    dm_policy = policy_from_extra(extra, "dm")
    grp_policy = policy_from_extra(extra, "group")

    # DM policy checks
    assert dm_policy.is_admin(dm_admin) is True
    assert dm_policy.is_admin(group_admin) is False
    assert dm_policy.can_run(group_admin, "status") is False

    # Group policy checks
    assert grp_policy.is_admin(group_admin) is True
    assert grp_policy.is_admin(dm_admin) is False
    assert grp_policy.can_run(dm_admin, "status") is False


# ---------------------------------------------------------------------------
# 6. Public commands and onboarding continue to work
# ---------------------------------------------------------------------------


def test_public_commands_and_onboarding_remain_accessible():
    # Both with unconfigured and configured admin allowlists
    for extra in ({}, {"allow_admin_from": ["968323641"], "user_allowed_commands": []}):
        policy = policy_from_extra(extra, "dm")
        for cmd in ("help", "whoami", "start", "menu"):
            assert policy.can_run("synthetic_user_999", cmd) is True


@pytest.mark.asyncio
async def test_public_whoami_dispatch_succeeds_for_non_admin():
    runner = _make_runner(platform_extra={"allow_admin_from": ["968323641"]})
    source = _make_source(user_id="synthetic_user_999")

    result = await runner._handle_message(_make_event("/whoami", source))
    assert result is not None
    assert "⛔" not in result
    assert "Tier: user" in result


# ---------------------------------------------------------------------------
# 7. Ordinary user preserves access to nutrition diary, profile, calorie targets
# ---------------------------------------------------------------------------


def test_user_data_and_nutrition_diary_profile_commands_preserved():
    for extra in ({}, {"allow_admin_from": ["968323641"], "user_allowed_commands": []}):
        policy = policy_from_extra(extra, "dm")
        for cmd in ("diary", "profile", "weight", "water", "stats", "undo_meal", "diary_undo"):
            assert policy.can_run("248875361", cmd) is True


# ---------------------------------------------------------------------------
# 8. Missing admin allowlist cannot bypass via running agent or alternate handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_agent_fast_path_cannot_bypass_admin_gate():
    runner = _make_runner(platform_extra={})
    source = _make_source(user_id="synthetic_user_999")

    # Simulate an active agent running on this session
    session_key = runner.session_store.get_or_create_session.return_value.session_key
    runner._running_agents[session_key] = MagicMock()

    # Attempt admin command on fast-path
    result = await runner._handle_message(_make_event("/restart", source))
    assert result is not None
    assert "⛔" in result
    assert "/restart is admin-only here" in result


def test_telegram_memory_stats_alternate_handler_fails_closed():
    from gateway.platforms.telegram import TelegramAdapter

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = PlatformConfig(enabled=True, token="***", extra={})
    adapter._memory_stats_extra_from_config_file = lambda: {}
    adapter._memory_stats_scope = lambda chat_type: "dm"

    msg = SimpleNamespace(
        chat=SimpleNamespace(id=1001, type="private"),
        from_user=SimpleNamespace(id=248875361),
    )
    assert adapter._memory_stats_is_admin(msg) is False


# ---------------------------------------------------------------------------
# 9. Invalid types and unexpected config values handled safely
# ---------------------------------------------------------------------------


def test_invalid_types_and_unexpected_config_values_handled_safely():
    # Coercing exotic, corrupted, and malformed inputs
    exotic_inputs = [
        {"allow_admin_from": 12345},
        {"allow_admin_from": "111,  222 , 333"},
        {"allow_admin_from": [111, "222", None, False, {"a": "b"}, "*"]},
        {"allow_admin_from": ("555", "666")},
    ]

    for extra in exotic_inputs:
        policy = policy_from_extra(extra, "dm")
        # Ensure it always produces a valid SlashAccessPolicy without raising
        assert isinstance(policy, SlashAccessPolicy)
        assert policy.is_admin(None) is False
        assert policy.is_admin(False) is False
        assert policy.is_admin("*") is False


# ---------------------------------------------------------------------------
# 10. Existing authorization parity
# ---------------------------------------------------------------------------


def test_existing_authorizations_parity():
    owner_id = "968323641"
    target_id = "248875361"
    extra = {
        "allow_admin_from": [owner_id],
        "group_allow_admin_from": [owner_id],
        "user_allowed_commands": ["help", "whoami"],
    }
    policy_dm = policy_from_extra(extra, "dm")
    policy_grp = policy_from_extra(extra, "group")

    # Owner has administrative parity
    assert policy_dm.is_admin(owner_id) is True
    assert policy_grp.is_admin(owner_id) is True

    # Target ID is denied
    assert policy_dm.is_admin(target_id) is False
    assert policy_grp.is_admin(target_id) is False
