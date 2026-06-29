from __future__ import annotations

import logging
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import hermes_logging
from agent.redact import RedactingFormatter
from agent.turn_context import build_turn_context
from gateway.config import Platform
from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    _log_safe_base_session_event,
)
from gateway.run import (
    _log_healbite_diary_command_failure,
    _log_inbound_message_event,
    _safe_agent_hook_content_fields,
    _log_pre_gateway_dispatch_skip,
    _log_safe_session_event,
    _log_telegram_notification_failure,
    _log_telegram_notification_injected,
    _log_telegram_topic_recovery,
)
from gateway.session import SessionSource
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.healbite_weight_tracker import HealBiteWeightTracker
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
    "PII_CHAT_ID_731000001",
    "PII_USER_ID_731000002",
    "PII_THREAD_ID_731000003",
    "PII_MESSAGE_ID_731000004",
    "PII_TOPIC_ID_731000005",
    "PII_WATCH_PATTERN_SHOULD_NOT_APPEAR",
    "PII_SESSION_CHAT_741000001",
    "PII_SESSION_USER_741000002",
    "PII_SESSION_THREAD_741000003",
    "telegram:741000001:741000002",
    "PII_V2_TEXT_28JUNE",
    "PII_V2_CAPTION_28JUNE",
    "PII_V2_TOOL_ARGUMENT_28JUNE",
    "PII_V2_CALLBACK_SECRET_28JUNE",
    "PII_V2_SESSION_CHAT_751000001",
    "PII_V2_SESSION_USER_751000002",
    "PII_V2_SESSION_THREAD_751000003",
    "PII_V2_EXCEPTION_28JUNE",
    "PII_V2_QDRANT_PAYLOAD_28JUNE",
    "PII_V3_CHAT_761000001",
    "PII_V3_USER_761000002",
    "PII_V3_DRAFT_761000003",
    "PII_V3_SESSION_761000004",
    "PII_V3_CHOICE_SHOULD_NOT_APPEAR",
    "PII_V3_EXCEPTION_BODY_SHOULD_NOT_APPEAR",
    "PII_V3_HANDOFF_EXCEPTION_SHOULD_NOT_APPEAR",
    "PII_S70C_WEIGHT_VALUE_821",
    "PII_S70C_PROFILE_FIELD",
    "PII_S70C_REMINDER_TIME",
    "PII_S70C_RECIPIENT",
    "PII_S70C_CALLBACK",
    "PII_S70C_EXCEPTION_BODY",
    "PII_S70C_SESSION_KEY",
    "PII_S70C_OBS_WEIGHT_831",
    "PII_S70C_OBS_PREVIOUS_WEIGHT",
    "PII_S70C_OBS_MACROS",
    "PII_S70C_OBS_PROFILE",
    "PII_S70C_OBS_DB_PATH",
    "PII_S70C_OBS_SESSION_KEY",
    "PII_S70C_OBS_EXCEPTION_BODY",
    "PII_S70C_OBS_TELEGRAM_ID",
    "PII_S70C1_TEXT_LOG_CANARY",
    "PII_S70C1_OUTBOUND_CANARY",
    "PII_S70C1_COMMAND_CANARY",
    "PII_S70C1_TOOL_PROMPT_CANARY",
    "PII_S70C1_CAPTION_CANARY",
    "PII_S70C1_CALLBACK_CANARY",
    "PII_S70C1_FILE_ID_CANARY",
    "PII_S70C1_EXCEPTION_CANARY",
    "PII_S70C1_NESTED_EXTRA_CANARY",
    "PII_S70C1_FORMAT_ARG_CANARY",
    "PII_EXTENDED_PROMPT_KEY",
    "PII_EXTENDED_QUERY_KEY",
    "PII_EXTENDED_INPUT_TEXT_KEY",
    "PII_EXTENDED_USER_TEXT_KEY",
    "PII_EXTENDED_CONTENT_KEY",
    "PII_EXTENDED_TOOL_ARGUMENTS_KEY",
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


def _install_file_logging(tmp_path: Path, *, log_level: str = "INFO"):
    root = logging.getLogger()
    existing = list(root.handlers)
    previous_initialized = hermes_logging._logging_initialized
    hermes_logging.setup_logging(
        hermes_home=tmp_path,
        mode="gateway",
        force=True,
        log_level=log_level,
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


def test_telegram_recovery_and_notification_logs_redact_identifier_canaries(tmp_path):
    cleanup = _install_file_logging(tmp_path)
    try:
        _log_pre_gateway_dispatch_skip(
            platform="telegram",
            reason="PII_WATCH_PATTERN_SHOULD_NOT_APPEAR",
            has_chat=True,
        )
        _log_telegram_topic_recovery(
            "PII_TOPIC_ID_731000005",
            "PII_THREAD_ID_731000003",
        )
        _log_telegram_notification_injected(
            notification_type="watch",
            platform="telegram",
            has_thread=True,
            outcome="queued",
        )
        _log_telegram_notification_failure(
            notification_type="watch",
            platform="telegram",
            has_thread=True,
            exc=RuntimeError(
                "PII_CHAT_ID_731000001 PII_USER_ID_731000002 "
                "PII_WATCH_PATTERN_SHOULD_NOT_APPEAR"
            ),
        )
        _log_telegram_notification_injected(
            notification_type="process",
            platform="telegram",
            has_thread=False,
            outcome="queued",
        )
        _log_telegram_notification_failure(
            notification_type="process",
            platform="telegram",
            has_thread=False,
            exc=RuntimeError(
                "PII_MESSAGE_ID_731000004 PII_TOPIC_ID_731000005"
            ),
        )
        _log_healbite_diary_command_failure(
            "slash_diary",
            RuntimeError("PII_USER_ID_731000002"),
        )

        logs = _read_file_logs(tmp_path)
        assert "pre_gateway_dispatch_skip" in logs
        assert "telegram_topic_recovery" in logs
        assert "telegram_notification_injected notification_type=watch" in logs
        assert "telegram_notification_injected notification_type=process" in logs
        assert "recovery_strategy=topic_binding_lookup" in logs
        assert "has_previous_topic=true" in logs
        assert "has_recovered_topic=true" in logs
        assert "has_thread=true" in logs
        assert "has_thread=false" in logs
        assert "outcome=queued" in logs
        assert "outcome=error" in logs
        assert "error_type=RuntimeError" in logs
        assert "command=slash_diary" in logs
        _assert_no_canaries(logs)
    finally:
        cleanup()


def test_session_diagnostics_use_safe_markers_without_session_canaries(tmp_path):
    cleanup = _install_file_logging(tmp_path)
    try:
        _log_safe_session_event(
            "session_diag_test",
            session_value="telegram:741000001:741000002",
            session_scope="update_watch",
            fields={
                "notification_type": "watch",
                "route": "process",
                "outcome": "queued",
                "has_thread": "true",
                "has_prompt": "true",
                "prompt_length": len("PII_SESSION_THREAD_741000003"),
            },
        )
        _log_safe_session_event(
            "session_diag_text_test",
            session_value="PII_SESSION_CHAT_741000001",
            session_scope="interrupt",
            level=logging.WARNING,
            fields={
                "has_text": "true",
                "text_length": len("PII_SESSION_USER_741000002"),
            },
        )

        logs = _read_file_logs(tmp_path)
        assert "session_diag_test" in logs
        assert "session_diag_text_test" in logs
        assert "has_session=true" in logs
        assert "session_scope=update_watch" in logs
        assert "session_scope=interrupt" in logs
        assert "notification_type=watch" in logs
        assert "route=process" in logs
        assert "outcome=queued" in logs
        assert "has_text=true" in logs
        assert "text_length=" in logs
        _assert_no_canaries(logs)
    finally:
        cleanup()



def test_gateway_run_source_omits_known_raw_session_log_formats():
    source = Path("gateway/run.py").read_text(encoding="utf-8")
    banned_formats = [
        "Gateway intercepted clarify text response (session=%s, id=%s)",
        "Ignoring /start platform ping for session %s",
        "Rejecting new active session %s: max_concurrent_sessions reached",
        "[Gateway] Auto-loaded skill(s) %s for session %s",
        "Backup interrupt detected for session %s",
        "Agent idle for %.0fs (timeout %.0fs) in session %s",
    ]
    for banned in banned_formats:
        assert banned not in source


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


def test_remaining_telegram_file_log_blockers_emit_safe_markers_only(tmp_path):
    cleanup = _install_file_logging(tmp_path, log_level="DEBUG")
    try:
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
        adapter._log_telegram_draft_diagnostic(
            operation="draft_update",
            outcome="failed",
            error=RuntimeError(
                "PII_V3_CHAT_761000001 PII_V3_DRAFT_761000003 "
                "PII_V3_EXCEPTION_BODY_SHOULD_NOT_APPEAR"
            ),
        )
        adapter._log_telegram_draft_diagnostic(
            operation="draft_lookup",
            outcome="miss",
            has_draft=False,
        )
        adapter._log_telegram_approval_resolution(
            choice="PII_V3_CHOICE_SHOULD_NOT_APPEAR",
            count=1,
            outcome="resolved",
        )
        adapter._log_telegram_approval_resolution(
            choice="once",
            count=0,
            outcome="failed",
            error=RuntimeError(
                "PII_V3_USER_761000002 PII_V3_EXCEPTION_BODY_SHOULD_NOT_APPEAR"
            ),
        )
        _log_safe_session_event(
            "handoff_processing_result",
            session_value="PII_V3_SESSION_761000004",
            session_scope="handoff_dispatch",
            fields={
                "operation": "handoff",
                "outcome": "failed",
                "error_type": "RuntimeError",
            },
        )

        _log_safe_base_session_event(
            "telegram",
            operation="debounce_candidate",
            outcome="accepted",
            session_value="PII_V3_SESSION_761000004",
            session_scope="queue_text_debounce",
            fields={"text_len": len("PII_V3_SESSION_761000004")},
        )
        _log_safe_base_session_event(
            "telegram",
            operation="cancel_active_processing",
            outcome="started",
            session_value="PII_V3_SESSION_761000004",
            session_scope="cancel_active_processing",
        )
        _log_safe_base_session_event(
            "telegram",
            operation="photo_followup",
            outcome="queued",
            session_value="PII_V3_SESSION_761000004",
            session_scope="photo_followup_queue",
        )

        logs = _read_file_logs(tmp_path)
        assert "telegram_draft_diagnostic" in logs
        assert "telegram_approval_resolution" in logs
        assert "operation=draft_update" in logs
        assert "operation=draft_lookup" in logs
        assert "has_draft=true" in logs
        assert "has_draft=false" in logs
        assert "route=approval_callback" in logs
        assert "choice_type=dynamic" in logs
        assert "action=once" in logs
        assert "has_choice=true" in logs
        assert "has_session=true" in logs
        assert "session_scope=approval_callback" in logs
        assert "session_scope=handoff_dispatch" in logs
        assert "operation=handoff" in logs
        assert "base_session_event" in logs
        assert "session_scope=queue_text_debounce" in logs
        assert "session_scope=cancel_active_processing" in logs
        assert "session_scope=photo_followup_queue" in logs
        assert "error_type=RuntimeError" in logs
        _assert_no_canaries(logs)
    finally:
        cleanup()



def test_weight_record_file_logs_keep_safe_markers_without_health_values_or_canaries(tmp_path, monkeypatch):
    cleanup = _install_file_logging(tmp_path)
    try:
        db_path = tmp_path / "PII_S70C_OBS_DB_PATH.db"
        store = HealBiteUserProfileStore(db_path=db_path)
        store.upsert_user_profile(
            user_id=101,
            username="PII_S70C_OBS_PROFILE",
            sex="male",
            age=35,
            height_cm=180,
            weight_kg=80,
            goal="maintain",
            activity_level="moderate",
            manual_kcal_target=2000,
        )
        store.recalculate_profile_targets(user_id=101, target_source="manual", manual_kcal_target=2000)
        tracker = HealBiteWeightTracker(db_path=db_path)
        monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)

        def _boom(*args, **kwargs):
            raise RuntimeError(
                "PII_S70C_OBS_WEIGHT_831 "
                "PII_S70C_OBS_PREVIOUS_WEIGHT "
                "PII_S70C_OBS_MACROS "
                "PII_S70C_OBS_PROFILE "
                "PII_S70C_OBS_DB_PATH "
                "PII_S70C_OBS_SESSION_KEY "
                "PII_S70C_OBS_EXCEPTION_BODY "
                "PII_S70C_OBS_TELEGRAM_ID"
            )

        with patch("gateway.healbite_user_profile.calculate_nutrition_targets", _boom):
            tracker.add_weight_entry(101, 82.4, source="telegram_command", corr="abc123def456")

        logs = _read_file_logs(tmp_path)
        assert "[HealBite][weight_record]" in logs
        assert "route=weight" in logs
        assert "action=record" in logs
        assert "outcome=recalculation_failed" in logs
        assert "weight_saved=true" in logs
        assert "has_previous_weight=true" in logs
        assert "recalculation_attempted=true" in logs
        assert "recalculation_completed=false" in logs
        assert "targets_changed=false" in logs
        assert "error_type=RuntimeError" in logs
        assert "corr_present=true" in logs
        assert "corr=abc123def456" in logs
        _assert_no_canaries(logs)
    finally:
        cleanup()



def test_telegram_agent_hook_content_fields_use_shape_without_raw_text():
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="4242424242",
        chat_type="dm",
        user_id="3131313131",
        user_name="PII_USERNAME_SHOULD_NOT_APPEAR",
    )

    fields = _safe_agent_hook_content_fields(
        source=source,
        field="message",
        content="PII_S70C1_TEXT_LOG_CANARY",
    )

    assert fields["message"] == ""
    assert fields["message_content_type"] == "text"
    assert fields["message_text_length"] == len("PII_S70C1_TEXT_LOG_CANARY")
    assert fields["message_image_count"] == 0
    _assert_no_canaries(str(fields))


def test_file_log_formatter_redacts_structured_telegram_text_args_and_exceptions(tmp_path):
    cleanup = _install_file_logging(tmp_path, log_level="DEBUG")
    try:
        logger = logging.getLogger("agent.privacy_regression")
        logger.info(
            "telegram turn message=%s caption=%s callback_data=%s file_id=%s response=%s",
            "PII_S70C1_TEXT_LOG_CANARY",
            "PII_S70C1_CAPTION_CANARY",
            "PII_S70C1_CALLBACK_CANARY",
            "PII_S70C1_FILE_ID_CANARY",
            "PII_S70C1_OUTBOUND_CANARY",
        )
        logger.info(
            "hook context %s",
            {
                "message": "PII_S70C1_FORMAT_ARG_CANARY",
                "payload": {"text": "PII_S70C1_NESTED_EXTRA_CANARY"},
                "route": "profile",
                "outcome": "allowed",
            },
        )
        extra_record = logging.LogRecord(
            name="gateway.privacy_extra",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="extra fields route=profile outcome=allowed",
            args=(),
            exc_info=None,
        )
        extra_record.payload = "PII_S70C1_NESTED_EXTRA_CANARY"
        formatter = RedactingFormatter("%(message)s payload=%(payload)s")
        rendered_extra = formatter.format(extra_record)
        assert "route=profile" in rendered_extra
        assert "outcome=allowed" in rendered_extra
        assert "PII_S70C1_NESTED_EXTRA_CANARY" not in rendered_extra

        exception_canary = "PII_S70C1_EXCEPTION_CANARY"
        try:
            raise RuntimeError(exception_canary)
        except RuntimeError as exc:
            logger.warning("telegram processing exception=%s", exc, exc_info=True)

        logs = _read_file_logs(tmp_path)
        assert "hook context" in logs
        assert "message=<redacted content>" in logs
        assert "caption=<redacted content>" in logs
        assert "callback_data=<redacted content>" in logs
        assert "file_id=<redacted content>" in logs
        assert "response=<redacted content>" in logs
        assert "RuntimeError: <redacted exception>" in logs
        assert logs.strip()
        _assert_no_canaries(logs)
    finally:
        cleanup()



def test_file_log_formatter_redacts_extended_user_content_keys_without_neighbor_false_positives(tmp_path):
    cleanup = _install_file_logging(tmp_path, log_level="DEBUG")
    try:
        logger = logging.getLogger("gateway.extended_privacy_regression")
        logger.info(
            "extended direct prompt=%s query=%s input_text=%s user_text=%s content=%s tool_arguments=%s "
            "content_type=text content_length=42 query_count=2 query_present=true "
            "prompt_present=true prompt_length=64 input_text_present=true "
            "tool_arguments_present=true message_kind=text has_previous_weight=true "
            "history_count_bucket=nonempty corr=abc123",
            "PII_EXTENDED_PROMPT_KEY",
            "PII_EXTENDED_QUERY_KEY",
            "PII_EXTENDED_INPUT_TEXT_KEY",
            "PII_EXTENDED_USER_TEXT_KEY",
            "PII_EXTENDED_CONTENT_KEY",
            "PII_EXTENDED_TOOL_ARGUMENTS_KEY",
        )
        logger.info(
            "extended quoted prompt=\"PII_EXTENDED_PROMPT_KEY with spaces\" "
            "query='PII_EXTENDED_QUERY_KEY with punctuation!' "
            "content=PII_EXTENDED_CONTENT_KEY"
        )
        logger.info(
            "extended mapping prompt=%(prompt)s query=%(query)s input_text=%(input_text)s "
            "user_text=%(user_text)s content=%(content)s tool_arguments=%(tool_arguments)s",
            {
                "prompt": "PII_EXTENDED_PROMPT_KEY",
                "query": "PII_EXTENDED_QUERY_KEY",
                "input_text": "PII_EXTENDED_INPUT_TEXT_KEY",
                "user_text": "PII_EXTENDED_USER_TEXT_KEY",
                "content": "PII_EXTENDED_CONTENT_KEY",
                "tool_arguments": "PII_EXTENDED_TOOL_ARGUMENTS_KEY",
            },
        )
        logger.info(
            "extended nested %s",
            {
                "prompt": "PII_EXTENDED_PROMPT_KEY",
                "query": "PII_EXTENDED_QUERY_KEY",
                "items": [
                    {"input_text": "PII_EXTENDED_INPUT_TEXT_KEY"},
                    {"user_text": "PII_EXTENDED_USER_TEXT_KEY"},
                    {"content": "PII_EXTENDED_CONTENT_KEY"},
                    {"tool_arguments": "PII_EXTENDED_TOOL_ARGUMENTS_KEY"},
                ],
                "route": "weight",
                "action": "history",
                "outcome": "ok",
                "content_type": "text",
            },
        )

        extra_record = logging.LogRecord(
            name="gateway.extended_privacy_extra",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="extra extended route=weight action=history outcome=ok",
            args=(),
            exc_info=None,
        )
        extra_record.prompt = "PII_EXTENDED_PROMPT_KEY"
        extra_record.query = "PII_EXTENDED_QUERY_KEY"
        extra_record.input_text = "PII_EXTENDED_INPUT_TEXT_KEY"
        extra_record.user_text = "PII_EXTENDED_USER_TEXT_KEY"
        extra_record.content = "PII_EXTENDED_CONTENT_KEY"
        extra_record.tool_arguments = "PII_EXTENDED_TOOL_ARGUMENTS_KEY"
        extra_record.content_type = "text"
        extra_record.query_count = 2
        extra_record.prompt_length = 64
        extra_record.corr = "abc123"
        formatter = RedactingFormatter(
            "%(message)s prompt=%(prompt)s query=%(query)s input_text=%(input_text)s "
            "user_text=%(user_text)s content=%(content)s tool_arguments=%(tool_arguments)s "
            "content_type=%(content_type)s query_count=%(query_count)s "
            "prompt_length=%(prompt_length)s corr=%(corr)s"
        )
        rendered_extra = formatter.format(extra_record)
        assert "prompt=<redacted content>" in rendered_extra
        assert "query=<redacted content>" in rendered_extra
        assert "input_text=<redacted content>" in rendered_extra
        assert "user_text=<redacted content>" in rendered_extra
        assert "content=<redacted content>" in rendered_extra
        assert "tool_arguments=<redacted content>" in rendered_extra
        assert "content_type=text" in rendered_extra
        assert "query_count=2" in rendered_extra
        assert "prompt_length=64" in rendered_extra
        assert "corr=abc123" in rendered_extra
        _assert_no_canaries(rendered_extra)

        exception_canary = "PII_EXTENDED_CONTENT_KEY"
        try:
            raise RuntimeError(exception_canary)
        except RuntimeError as exc:
            logger.warning("extended exception content=%s", exc, exc_info=True)

        logs = _read_file_logs(tmp_path)
        assert logs.strip()
        assert "extended direct" in logs
        assert "extended quoted" in logs
        assert "extended mapping" in logs
        assert "extended nested" in logs
        assert "prompt=<redacted content>" in logs
        assert "query=<redacted content>" in logs
        assert "input_text=<redacted content>" in logs
        assert "user_text=<redacted content>" in logs
        assert "content=<redacted content>" in logs
        assert "tool_arguments=<redacted content>" in logs
        assert "content_type=text" in logs
        assert "content_length=42" in logs
        assert "query_count=2" in logs
        assert "query_present=true" in logs
        assert "prompt_present=true" in logs
        assert "prompt_length=64" in logs
        assert "input_text_present=true" in logs
        assert "tool_arguments_present=true" in logs
        assert "message_kind=text" in logs
        assert "has_previous_weight=true" in logs
        assert "history_count_bucket=nonempty" in logs
        assert "corr=abc123" in logs
        assert "RuntimeError: <redacted exception>" in logs
        _assert_no_canaries(logs)
    finally:
        cleanup()
