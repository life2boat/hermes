from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "healbite_cli.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("healbite_cli", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
healbite_cli = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = healbite_cli
SCRIPT_SPEC.loader.exec_module(healbite_cli)


def test_build_parser_parses_logs_command():
    parser = healbite_cli.build_parser()
    args = parser.parse_args(["logs", "--last", "50"])
    assert args.command == "logs"
    assert args.last == 50


def test_build_parser_parses_simulate_message_user_id():
    parser = healbite_cli.build_parser()
    args = parser.parse_args([
        "simulate-message",
        "/diary 7d",
        "--user-id",
        "248875361",
    ])
    assert args.command == "simulate-message"
    assert args.text == "/diary 7d"
    assert args.user_id == 248875361


def test_filter_log_lines_keeps_only_diagnostic_matches_and_redacts_secrets():
    raw = "\n".join([
        "plain info line",
        "provider authentication failed api_key=shhh-secret",
        "nutrition_log write succeeded",
        "Authorization: Bearer abc.def.ghi",
    ])
    filtered = healbite_cli.filter_log_lines(raw)
    assert len(filtered) == 2
    assert all("plain info line" not in line for line in filtered)
    joined = "\n".join(filtered)
    assert "shhh-secret" not in joined
    assert "abc.def.ghi" not in joined
    assert "[REDACTED]" in joined


def test_fix_plan_output_contains_expected_checks():
    report = healbite_cli.build_fix_plan("provider-auth")
    assert "Issue: provider-auth" in report
    assert "gateway/run.py" in report
    assert "./scripts/healbite status" in report
    assert "bash scripts/agent_check.sh" in report


def test_simulate_message_rejects_unsupported_external_calls_by_default():
    report = healbite_cli.simulate_local_message("what is on this photo?")
    assert "Unsupported for local simulation" in report
    assert "LLM and external calls are disabled by default" in report


def test_render_status_report_never_prints_secret_values():
    report = healbite_cli.render_status_report({
        "git_status": "",
        "recent_commits": ["abc123 test"],
        "container_status": "running",
        "restart_count": 0,
        "runtime": {
            "hermes_home": "/home/hermes/.hermes",
            "config_path": "/home/hermes/.hermes/config.yaml",
            "env_path": "/home/hermes/.hermes/.env",
            "model_provider": "deepseek",
            "model_default": "deepseek-chat",
            "vision_provider": "gemini",
            "vision_model": "gemini-2.5-flash",
            "vision_ready": True,
            "db_path": "/home/hermes/healbite.db",
            "nutrition_log_count": 42,
            "admin_total_unique": 2,
            "allow_admin_from": ["968323641"],
            "group_allow_admin_from": ["248875361"],
            "secret_presence": {
                "GEMINI_API_KEY": True,
                "DEEPSEEK_API_KEY": True,
                "TELEGRAM_BOT_TOKEN": True,
            },
            "qdrant_presence": {
                "QDRANT_URL": True,
                "QDRANT_API_KEY": True,
            },
        },
        "write_probe": {
            "ok": True,
            "detail": "api_key=super-secret-value",
        },
    })
    assert "super-secret-value" not in report
    assert "GEMINI_API_KEY: yes" in report
    assert "[REDACTED]" in report
