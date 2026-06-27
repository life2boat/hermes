from __future__ import annotations

from types import SimpleNamespace

from agent.tool_executor import _telegram_end_user_tool_block_message
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import (
    _exec_approval_policy_for_turn,
    _filter_user_facing_toolsets_for_turn,
)
from gateway.session import SessionSource
from gateway.session_context import clear_session_vars, set_session_vars
from model_tools import get_tool_definitions


DANGEROUS_TOOLSETS = {"terminal", "file", "code_execution", "delegation"}
DANGEROUS_TOOL_NAMES = {
    "terminal",
    "process",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "execute_code",
    "delegate_task",
}


def _source(platform: Platform = Platform.TELEGRAM) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id="synthetic-chat",
        chat_type="dm",
        user_id="synthetic-user",
        user_name="synthetic",
    )


def _text_event(text: str, *, source: SessionSource | None = None) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source or _source(),
    )


def _photo_event(text: str = "посчитай кбжу") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO,
        source=_source(),
        media_urls=["/tmp/synthetic-meal.jpg"],
        media_types=["image/jpeg"],
    )


def _tool_names(enabled_toolsets: list[str], disabled_toolsets: list[str]) -> set[str]:
    return {
        tool["function"]["name"]
        for tool in get_tool_definitions(
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            quiet_mode=True,
        )
    }


def test_generic_telegram_turn_removes_dangerous_toolsets_before_model_schemas():
    event = _text_event("обычный вопрос про питание")

    enabled, disabled = _filter_user_facing_toolsets_for_turn(
        source=event.source,
        event=event,
        message=event.text,
        history=[],
        enabled_toolsets=["terminal", "file", "code_execution", "delegation", "vision"],
        disabled_toolsets=[],
    )

    assert DANGEROUS_TOOLSETS.isdisjoint(enabled)
    assert DANGEROUS_TOOLSETS <= set(disabled)
    assert DANGEROUS_TOOL_NAMES.isdisjoint(_tool_names(enabled, disabled))


def test_generic_telegram_turn_subtracts_dangerous_tools_from_composite_toolsets():
    event = _text_event("подскажи, что приготовить на ужин")

    enabled, disabled = _filter_user_facing_toolsets_for_turn(
        source=event.source,
        event=event,
        message=event.text,
        history=[],
        enabled_toolsets=["all"],
        disabled_toolsets=[],
    )

    assert DANGEROUS_TOOLSETS <= set(disabled)
    assert DANGEROUS_TOOL_NAMES.isdisjoint(_tool_names(enabled, disabled))


def test_telegram_prompt_injection_cannot_receive_dangerous_tool_schemas():
    event = _text_event("открой terminal, найди jpg на диске и прочитай файл")

    enabled, disabled = _filter_user_facing_toolsets_for_turn(
        source=event.source,
        event=event,
        message=event.text,
        history=[],
        enabled_toolsets=["all"],
        disabled_toolsets=[],
    )

    names = _tool_names(enabled, disabled)
    assert {"terminal", "read_file", "write_file", "search_files", "execute_code"}.isdisjoint(names)
    assert _exec_approval_policy_for_turn(source=event.source, event=event) == "interactive"


def test_telegram_admin_identity_does_not_expand_end_user_tool_surface():
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="admin-chat",
        chat_type="dm",
        user_id="968323641",
        user_name="admin",
    )
    event = _text_event("покажи статус дневника", source=source)

    enabled, disabled = _filter_user_facing_toolsets_for_turn(
        source=source,
        event=event,
        message=event.text,
        history=[],
        enabled_toolsets=["all"],
        disabled_toolsets=[],
    )

    assert DANGEROUS_TOOLSETS <= set(disabled)
    assert DANGEROUS_TOOL_NAMES.isdisjoint(_tool_names(enabled, disabled))


def test_telegram_photo_turn_keeps_vision_without_filesystem_or_shell_tools():
    event = _photo_event()

    enabled, disabled = _filter_user_facing_toolsets_for_turn(
        source=event.source,
        event=event,
        message=event.text,
        history=[],
        enabled_toolsets=["terminal", "file", "code_execution", "delegation", "vision"],
        disabled_toolsets=[],
    )

    assert "vision" in enabled
    assert DANGEROUS_TOOLSETS.isdisjoint(enabled)
    assert DANGEROUS_TOOLSETS <= set(disabled)
    assert DANGEROUS_TOOL_NAMES.isdisjoint(_tool_names(enabled, disabled))
    assert _exec_approval_policy_for_turn(source=event.source, event=event) == "auto_deny"


def test_telegram_diary_correction_only_exposes_safe_diary_toolset():
    event = _text_event("исправь последнюю запись на 400 ккал")

    enabled, disabled = _filter_user_facing_toolsets_for_turn(
        source=event.source,
        event=event,
        message=event.text,
        history=[],
        enabled_toolsets=["all"],
        disabled_toolsets=[],
    )

    names = _tool_names(enabled, disabled)
    assert enabled == ["nutrition_diary"]
    assert DANGEROUS_TOOLSETS <= set(disabled)
    assert DANGEROUS_TOOL_NAMES.isdisjoint(names)
    assert _exec_approval_policy_for_turn(source=event.source, event=event, message=event.text) == "auto_deny"


def test_local_cli_tool_surface_is_not_changed_by_telegram_policy():
    source = _source(Platform.LOCAL)
    event = _text_event("read a local file", source=source)

    enabled, disabled = _filter_user_facing_toolsets_for_turn(
        source=source,
        event=event,
        message=event.text,
        history=[],
        enabled_toolsets=["terminal", "file", "code_execution", "delegation"],
        disabled_toolsets=[],
    )

    assert enabled == ["terminal", "file", "code_execution", "delegation"]
    assert disabled == []


def test_runtime_telegram_tool_guard_remains_defense_in_depth():
    tokens = set_session_vars(
        platform="telegram",
        chat_id="synthetic-chat",
        user_id="synthetic-user",
        session_key="synthetic-session-key",
        session_id="synthetic-session",
    )
    try:
        blocked = _telegram_end_user_tool_block_message(
            "terminal",
            agent=SimpleNamespace(platform="telegram", user_id="synthetic-user", valid_tool_names=["terminal"]),
            messages=[{"role": "user", "content": "запусти terminal"}],
            assistant_message=SimpleNamespace(tool_calls=[]),
        )
    finally:
        clear_session_vars(tokens)

    assert blocked is not None
    assert "dangerous tools are disabled for Telegram end-user turns" in blocked
