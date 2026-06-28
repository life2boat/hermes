from __future__ import annotations

import logging
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import hermes_logging
from agent.turn_context import build_turn_context
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import _log_inbound_message_event
from gateway.session import SessionSource
from gateway.memory.qdrant_adapter import QdrantMemoryAdapter
from gateway.platforms.telegram import TelegramAdapter
from gateway.config import PlatformConfig


PII_CANARIES = {
    "PII_LOG_SMOKE_TEXT_27JUNE",
    "PII_LOG_SMOKE_CAPTION_27JUNE",
    "PII_LOG_SMOKE_TOOL_ARGUMENT",
    "PII_LOG_SMOKE_CALLBACK_SECRET",
    "3131313131",
    "4242424242",
    "PII_USERNAME_SHOULD_NOT_APPEAR",
}


class _FakeTodoStore:
    def has_items(self):
        return False

    def _hydrate(self, *_a, **_k):
        return None


class _FakeGuardrails:
    def reset_for_turn(self):
        return None


class _FakeAgent:
    def __init__(self):
        self.session_id = "synthetic-session"
        self.model = "synthetic-model"
        self.provider = "synthetic-provider"
        self.base_url = "https://example.invalid"
        self.api_key = "sk-synthetic"
        self.api_mode = "chat_completions"
        self.platform = "telegram"
        self.quiet_mode = False
        self.max_iterations = 5
        self.tools = []
        self.valid_tool_names = set()
        self.compression_enabled = False
        self.context_compressor = types.SimpleNamespace(
            protect_first_n=2, protect_last_n=2
        )
        self._cached_system_prompt = "SYSTEM"
        self._memory_store = None
        self._memory_manager = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = _FakeTodoStore()
        self._tool_guardrails = _FakeGuardrails()
        self._compression_warning = None
        self._interrupt_requested = False
        self._memory_write_origin = "assistant_tool"
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None
        self.safe_print_calls: list[str] = []
        self._persist_calls = 0

    def _ensure_db_session(self):
        return None

    def _restore_primary_runtime(self):
        return None

    def _cleanup_dead_connections(self):
        return False

    def _emit_status(self, _msg):
        return None

    def _replay_compression_warning(self):
        return None

    def _hydrate_todo_store(self, *_a, **_k):
        return None

    def _safe_print(self, msg, *_a, **_k):
        self.safe_print_calls.append(str(msg))

    def _persist_session(self, *_a, **_k):
        self._persist_calls += 1


def _install_file_logging(tmp_path: Path):
    root = logging.getLogger()
    existing = list(root.handlers)
    previous_initialized = hermes_logging._logging_initialized
    hermes_logging.setup_logging(
        hermes_home=tmp_path,
        mode="gateway",
        force=True,
        log_level="INFO",
    )

    def cleanup():
        for handler in list(root.handlers):
            if handler not in existing:
                handler.flush()
                handler.close()
                root.removeHandler(handler)
        hermes_logging._logging_initialized = previous_initialized

    return cleanup


def _read_file_logs(tmp_path: Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    log_dir = tmp_path / "logs"
    chunks: list[str] = []
    for name in ("agent.log", "gateway.log", "errors.log"):
        path = log_dir / name
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def _assert_no_canaries(text: str) -> None:
    for canary in PII_CANARIES:
        assert canary not in text


def test_telegram_file_logs_store_message_shape_not_raw_user_content(tmp_path):
    cleanup = _install_file_logging(tmp_path)
    try:
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="4242424242",
            chat_type="dm",
            user_id="3131313131",
            user_name="PII_USERNAME_SHOULD_NOT_APPEAR",
        )
        event = MessageEvent(
            text=(
                "PII_LOG_SMOKE_TEXT_27JUNE "
                "PII_LOG_SMOKE_CAPTION_27JUNE "
                "PII_LOG_SMOKE_TOOL_ARGUMENT"
            ),
            message_type=MessageType.PHOTO,
            media_urls=["telegram-photo-cache.jpg"],
            media_types=["image/jpeg"],
            source=source,
        )

        _log_inbound_message_event(event, source)

        agent = _FakeAgent()
        with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
            build_turn_context(
                agent=agent,
                user_message=event.text,
                system_message=None,
                conversation_history=None,
                task_id=None,
                stream_callback=None,
                persist_user_message=None,
                restore_or_build_system_prompt=lambda *a, **k: None,
                install_safe_stdio=lambda: None,
                sanitize_surrogates=lambda s: s,
                summarize_user_message_for_log=lambda s: s,
                set_session_context=lambda _sid: None,
                set_current_write_origin=lambda _o: None,
                ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
            )

        logs = _read_file_logs(tmp_path)
        assert "inbound message:" in logs
        assert "conversation turn:" in logs
        assert "platform=telegram" in logs
        assert "content_type=" in logs
        assert "text_length=" in logs
        assert "media_count=1" in logs
        assert "image_count=0" in logs
        _assert_no_canaries(logs)
        _assert_no_canaries("\n".join(agent.safe_print_calls))
    finally:
        cleanup()



def test_telegram_route_markers_in_file_logs_keep_shape_and_redact_callbacks(tmp_path):
    cleanup = _install_file_logging(tmp_path)
    try:
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
        adapter.handle_message = AsyncMock()
        adapter._log_healbite_marker(
            "telegram_update_received",
            route="profile",
            outcome="allowed",
            content_type="text",
            has_text=True,
            text_length=len("PII_LOG_SMOKE_TEXT_27JUNE"),
            callback_data="PII_LOG_SMOKE_CALLBACK_SECRET",
            user_id="3131313131",
            chat_id="4242424242",
        )
        adapter._log_healbite_marker(
            "healbite_route_selected",
            route="keyboard_action",
            action="PII_LOG_SMOKE_CALLBACK_SECRET",
            lane="healbite_public",
            result="allowed",
        )

        logs = _read_file_logs(tmp_path)
        assert "telegram_update_received" in logs
        assert "healbite_route_selected" in logs
        assert "route=profile" in logs
        assert "route=keyboard_action" in logs
        assert "outcome=allowed" in logs
        assert "result=allowed" in logs
        assert "corr=" in logs
        assert "action=redacted" in logs
        _assert_no_canaries(logs)
    finally:
        cleanup()


def test_qdrant_file_log_failure_does_not_emit_payload_values(tmp_path):
    cleanup = _install_file_logging(tmp_path)
    try:
        class _Embedding:
            def embed_text(self, _text):
                return [0.0] * 32

        class _Client:
            def get_collection(self, _name):
                return {}

            def upsert(self, **_kwargs):
                raise RuntimeError("PII_LOG_SMOKE_TOOL_ARGUMENT")

        adapter = QdrantMemoryAdapter(
            enabled=True,
            url="http://qdrant.invalid:6333",
            collection_name="healbite_memory_os",
            embedding_adapter=_Embedding(),
            client_factory=lambda: _Client(),
        )

        ok = adapter.upsert_fact(
            sqlite_id=1,
            user_id=3131313131,
            text="PII_LOG_SMOKE_TEXT_27JUNE",
            payload={"note": "PII_LOG_SMOKE_CALLBACK_SECRET"},
        )

        logs = _read_file_logs(tmp_path)
        assert ok is False
        assert "failed to upsert semantic memory point" in logs
        assert "error_type=RuntimeError" in logs
        _assert_no_canaries(logs)
    finally:
        cleanup()


def test_telegram_multimodal_turn_file_log_uses_shape_only(tmp_path):
    cleanup = _install_file_logging(tmp_path)
    try:
        agent = _FakeAgent()
        user_message = [
            {"type": "text", "text": "PII_LOG_SMOKE_TEXT_27JUNE"},
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        "data:image/jpeg;base64,"
                        "PII_LOG_SMOKE_CALLBACK_SECRET"
                    )
                },
            },
        ]

        with patch("agent.auxiliary_client.set_runtime_main", lambda *a, **k: None):
            build_turn_context(
                agent=agent,
                user_message=user_message,
                system_message=None,
                conversation_history=None,
                task_id=None,
                stream_callback=None,
                persist_user_message=None,
                restore_or_build_system_prompt=lambda *a, **k: None,
                install_safe_stdio=lambda: None,
                sanitize_surrogates=lambda s: s,
                summarize_user_message_for_log=lambda s: str(s),
                set_session_context=lambda _sid: None,
                set_current_write_origin=lambda _o: None,
                ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *a, **k: None),
            )

        logs = _read_file_logs(tmp_path)
        assert "conversation turn:" in logs
        assert "content_type=multimodal" in logs
        assert "image_count=1" in logs
        _assert_no_canaries(logs)
        _assert_no_canaries("\n".join(agent.safe_print_calls))
    finally:
        cleanup()
