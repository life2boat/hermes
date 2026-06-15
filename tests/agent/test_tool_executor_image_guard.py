from types import SimpleNamespace

from agent.tool_executor import _image_analysis_tool_block_message


def _assistant_message(*tool_names: str):
    tool_calls = [
        SimpleNamespace(function=SimpleNamespace(name=name))
        for name in tool_names
    ]
    return SimpleNamespace(tool_calls=tool_calls)


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
