from types import SimpleNamespace

from agent.tool_executor import (
    _image_analysis_tool_block_message,
    _telegram_end_user_tool_block_message,
)
from gateway.session_context import clear_session_vars, set_session_vars


def _assistant_message(*tool_names: str):
    tool_calls = [
        SimpleNamespace(function=SimpleNamespace(name=name))
        for name in tool_names
    ]
    return SimpleNamespace(tool_calls=tool_calls)


def _telegram_agent(*tool_names: str):
    return SimpleNamespace(
        platform="telegram",
        user_id="248875361",
        valid_tool_names=list(tool_names),
    )


def test_terminal_is_blocked_for_image_context_messages():
    messages = [
        {
            "role": "user",
            "content": (
                "[The user sent an image: /tmp/meal.jpg]\n"
                "[If you need a closer look, use vision_analyze with image_url: /tmp/meal.jpg]"
            ),
        }
    ]

    blocked = _image_analysis_tool_block_message(
        "terminal",
        messages=messages,
        assistant_message=_assistant_message(),
    )

    assert blocked is not None
    assert "disabled for image-analysis tasks" in blocked


def test_execute_code_is_blocked_after_vision_tool_context():
    messages = [
        {
            "role": "tool",
            "name": "vision_analyze",
            "tool_name": "vision_analyze",
            "content": '{"success": false, "error": "Error analyzing image"}',
        }
    ]

    blocked = _image_analysis_tool_block_message(
        "execute_code",
        messages=messages,
        assistant_message=_assistant_message(),
    )

    assert blocked is not None
    assert "Do not inspect local image files" in blocked


def test_terminal_is_blocked_when_current_batch_requests_vision_analyze():
    blocked = _image_analysis_tool_block_message(
        "terminal",
        messages=[{"role": "user", "content": "Please inspect this"}],
        assistant_message=_assistant_message("vision_analyze", "terminal"),
    )

    assert blocked is not None


def test_non_image_turn_is_not_blocked():
    blocked = _image_analysis_tool_block_message(
        "terminal",
        messages=[{"role": "user", "content": "Run tests for the parser"}],
        assistant_message=_assistant_message(),
    )

    assert blocked is None


def test_non_blocked_tool_stays_available_even_on_image_turn():
    blocked = _image_analysis_tool_block_message(
        "read_file",
        messages=[{"role": "user", "content": "[The user sent an image: /tmp/meal.jpg]"}],
        assistant_message=_assistant_message("vision_analyze"),
    )

    assert blocked is None


def test_telegram_explicit_diary_correction_blocks_terminal_and_logs_audit_marker(caplog):
    caplog.set_level("WARNING")
    tokens = set_session_vars(
        platform="telegram",
        chat_id="chat-1",
        user_id="248875361",
        session_key="s-1",
        session_id="session-1",
    )
    try:
        blocked = _telegram_end_user_tool_block_message(
            "terminal",
            agent=_telegram_agent("update_last_meal", "terminal", "read_file", "execute_code"),
            messages=[
                {
                    "role": "user",
                    "content": "\u0438\u0441\u043f\u0440\u0430\u0432\u044c \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e \u0437\u0430\u043f\u0438\u0441\u044c \u043d\u0430 400 \u043a\u043a\u0430\u043b",
                }
            ],
            assistant_message=_assistant_message(),
        )
    finally:
        clear_session_vars(tokens)

    assert blocked is not None
    assert "update_last_meal" in blocked
    assert "dangerous tools are disabled for Telegram end-user turns" in blocked
    assert "diary_intent=explicit_correction" in caplog.text
    assert "requested_tool=terminal" in caplog.text


def test_telegram_read_only_diary_turn_blocks_read_file():
    tokens = set_session_vars(
        platform="telegram",
        chat_id="chat-2",
        user_id="248875361",
        session_key="s-2",
        session_id="session-2",
    )
    try:
        blocked = _telegram_end_user_tool_block_message(
            "read_file",
            agent=_telegram_agent("read_file", "update_last_meal"),
            messages=[
                {
                    "role": "user",
                    "content": "\u0447\u0442\u043e \u0443 \u043c\u0435\u043d\u044f \u0441\u0435\u0433\u043e\u0434\u043d\u044f \u0432 \u0434\u043d\u0435\u0432\u043d\u0438\u043a\u0435?",
                }
            ],
            assistant_message=_assistant_message(),
        )
    finally:
        clear_session_vars(tokens)

    assert blocked is not None
    assert "read-only diary request" in blocked


def test_telegram_ambiguous_diary_turn_blocks_execute_code():
    tokens = set_session_vars(
        platform="telegram",
        chat_id="chat-3",
        user_id="248875361",
        session_key="s-3",
        session_id="session-3",
    )
    try:
        blocked = _telegram_end_user_tool_block_message(
            "execute_code",
            agent=_telegram_agent("execute_code", "update_last_meal"),
            messages=[
                {
                    "role": "assistant",
                    "content": "\u0422\u0432\u043e\u0439 \u0434\u043d\u0435\u0432\u043d\u0438\u043a \u0437\u0430 \u0441\u0435\u0433\u043e\u0434\u043d\u044f: 400 \u043a\u043a\u0430\u043b",
                },
                {
                    "role": "user",
                    "content": "\u0438\u0441\u043f\u0440\u0430\u0432\u044c \u043e\u0448\u0438\u0431\u043a\u0443",
                },
            ],
            assistant_message=_assistant_message(),
        )
    finally:
        clear_session_vars(tokens)

    assert blocked is not None
    assert "clarify the exact correction" in blocked


def test_non_telegram_turn_is_not_blocked_by_telegram_user_guard():
    blocked = _telegram_end_user_tool_block_message(
        "terminal",
        agent=SimpleNamespace(platform="cli", user_id="", valid_tool_names=["terminal"]),
        messages=[{"role": "user", "content": "Run tests for the parser"}],
        assistant_message=_assistant_message(),
    )

    assert blocked is None
