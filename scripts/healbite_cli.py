#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONTAINER_NAME = "hermes-bot"
DEFAULT_LOG_TAIL = 80
SUPPORTED_SIMULATION_COMMANDS = {
    "/diary",
    "/diary 7d",
    "/stats",
    "/stats 7d",
    "/memory_stats",
    "/menu",
}
IMPORTANT_LOG_PATTERN = re.compile(
    r"(traceback|exception|provider authentication|command approval|execute_code|terminal|readonly|permissionerror|"
    r"database is locked|nutrition_log|diary|stats|vision|gemini|qdrant)",
    re.IGNORECASE,
)
SECRET_PATTERN = re.compile(
    r"(?P<key>\b(?:gemini|google|deepseek|telegram|openai)?_?(?:api_)?(?:key|token|secret|password)\b)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<value>[^\s\"']+)",
    re.IGNORECASE,
)
BEARER_PATTERN = re.compile(
    r"(Authorization:\s*Bearer\s+)([A-Za-z0-9._-]+)", re.IGNORECASE
)

FIX_PLANS: dict[str, dict[str, list[str] | str]] = {
    "provider-auth": {
        "title": "Provider authentication / quota failures",
        "root_cause": [
            "Confirm the runtime provider and auxiliary provider are what you expect.",
            "Check presence flags for API keys without printing values.",
            "Inspect recent logs for provider authentication, quota, or model-not-found errors.",
            "Confirm config.yaml did not inherit a stale base_url from another provider.",
        ],
        "files": [
            "scripts/healbite_cli.py",
            "gateway/run.py",
            "agent/auxiliary_client.py",
            "tools/vision_tools.py",
            "~/.hermes/config.yaml",
        ],
        "commands": [
            "./scripts/healbite status",
            "./scripts/healbite logs --last 120",
            "./scripts/healbite fix-plan --issue provider-auth",
        ],
        "tests": [
            "bash scripts/agent_check.sh",
            "pytest -q tests/scripts/test_healbite_cli.py",
        ],
        "deploy_gate": "No deploy until provider/auth cause is confirmed and user-facing fallback stays sanitized.",
    },
    "vision": {
        "title": "Vision recognition unavailable",
        "root_cause": [
            "Check auxiliary.vision.provider/model in runtime config.",
            "Verify check_vision_requirements() returns true.",
            "Inspect logs for sanitized provider/auth/model errors after a fresh image test.",
            "Confirm image routing is not falling back to non-vision paths.",
        ],
        "files": [
            "scripts/healbite_cli.py",
            "gateway/run.py",
            "gateway/platforms/telegram.py",
            "tools/vision_tools.py",
        ],
        "commands": [
            "./scripts/healbite status",
            "./scripts/healbite logs --last 120",
            "./scripts/healbite fix-plan --issue vision",
        ],
        "tests": [
            "bash scripts/agent_check.sh",
            "pytest -q tests/gateway/test_telegram_photo_vision_flow.py tests/scripts/test_healbite_cli.py",
        ],
        "deploy_gate": "Do not change provider/model or secrets from this CLI. Confirm runtime config separately if needed.",
    },
    "diary-readonly": {
        "title": "Diary DB is read-only or locked",
        "root_cause": [
            "Run the nutrition_log write probe as runtime uid=10000.",
            "Check for database is locked / PermissionError lines in logs.",
            "Confirm the DB path resolved at runtime matches the expected source of truth.",
            "Verify the nutrition_log schema exists before investigating command routing.",
        ],
        "files": [
            "scripts/healbite_cli.py",
            "gateway/healbite_nutrition_diary.py",
            "gateway/platforms/telegram.py",
        ],
        "commands": [
            "./scripts/healbite status",
            "./scripts/healbite test-diary",
            "./scripts/healbite logs --last 120",
        ],
        "tests": [
            "bash scripts/agent_check.sh",
            "pytest -q tests/gateway/test_healbite_nutrition_diary.py tests/scripts/test_healbite_cli.py",
        ],
        "deploy_gate": "Avoid destructive SQL. Any probe must insert and then delete only synthetic rows.",
    },
    "admin-acl": {
        "title": "Admin ACL / slash access policy issues",
        "root_cause": [
            "Inspect allow_admin_from and group_allow_admin_from from effective runtime config.",
            "Check policy_for_source() for DM and group scope separately.",
            "Confirm allowlists are loaded from config.yaml rather than assumed from environment.",
            "Verify command gating failures are not caused by per-platform scope mismatch.",
        ],
        "files": [
            "scripts/healbite_cli.py",
            "gateway/config.py",
            "gateway/slash_access.py",
            "gateway/run.py",
        ],
        "commands": [
            "./scripts/healbite status",
            "./scripts/healbite check-admins",
            "./scripts/healbite fix-plan --issue admin-acl",
        ],
        "tests": [
            "bash scripts/agent_check.sh",
            "pytest -q tests/gateway/test_slash_access.py tests/scripts/test_healbite_cli.py",
        ],
        "deploy_gate": "Do not mutate admin lists from the diagnostic CLI.",
    },
    "command-approval": {
        "title": "Unexpected Command Approval / terminal leakage",
        "root_cause": [
            "Inspect logs for execute_code, terminal, and Command Approval strings.",
            "Verify user-facing image and diary paths remove terminal/code/file toolsets.",
            "Check that safe fallbacks are emitted instead of raw provider or approval details.",
            "Confirm simulate-message stays local and does not invoke external tools by default.",
        ],
        "files": [
            "scripts/healbite_cli.py",
            "gateway/run.py",
            "gateway/platforms/telegram.py",
        ],
        "commands": [
            "./scripts/healbite logs --last 120",
            './scripts/healbite simulate-message "/menu"',
            "./scripts/healbite fix-plan --issue command-approval",
        ],
        "tests": [
            "bash scripts/agent_check.sh",
            "pytest -q tests/scripts/test_healbite_cli.py tests/gateway/test_telegram_photo_vision_flow.py",
        ],
        "deploy_gate": "Never expose terminal/code approval prompts in Telegram user-facing flows.",
    },
}


class CLIError(RuntimeError):
    """Expected CLI failure with a user-readable message."""


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class SubprocessRunner:
    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        result = CommandResult(proc.stdout, proc.stderr, proc.returncode)
        if check and proc.returncode != 0:
            raise CLIError(
                redact_secrets(
                    (proc.stderr or proc.stdout).strip() or "Command failed."
                )
            )
        return result


def redact_secrets(text: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        value = match.group("value").strip()
        if value.lower() in {"yes", "no", "true", "false", "[redacted]"}:
            return match.group(0)
        return f"{match.group('key')}{match.group('sep')}[REDACTED]"

    redacted = SECRET_PATTERN.sub(_replace, text)
    redacted = BEARER_PATTERN.sub(r"\1[REDACTED]", redacted)
    return redacted


def filter_log_lines(raw_text: str) -> list[str]:
    filtered: list[str] = []
    for line in raw_text.splitlines():
        if IMPORTANT_LOG_PATTERN.search(line):
            filtered.append(redact_secrets(line))
    return filtered


def build_fix_plan(issue: str) -> str:
    plan = FIX_PLANS.get(issue)
    if plan is None:
        known = ", ".join(sorted(FIX_PLANS))
        raise CLIError(f"Unsupported issue '{issue}'. Supported issues: {known}")
    lines = [f"Issue: {issue}", f"Focus: {plan['title']}", "", "Root-cause checklist:"]
    lines.extend(f"- {item}" for item in plan["root_cause"])
    lines.extend(["", "Files to inspect:"])
    lines.extend(f"- {item}" for item in plan["files"])
    lines.extend(["", "Commands to run:"])
    lines.extend(f"- {item}" for item in plan["commands"])
    lines.extend(["", "Tests to run:"])
    lines.extend(f"- {item}" for item in plan["tests"])
    lines.extend(["", f"Deploy gate: {plan['deploy_gate']}"])
    return "\n".join(lines)


def normalize_simulation_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def simulate_local_message(text: str, *, user_id: int | None = None) -> str:
    normalized = normalize_simulation_text(text)
    lowered = normalized.lower()
    if lowered not in SUPPORTED_SIMULATION_COMMANDS:
        supported = ", ".join(sorted(SUPPORTED_SIMULATION_COMMANDS))
        return (
            f"Unsupported for local simulation: {normalized or '<empty>'}\n"
            "LLM and external calls are disabled by default for this diagnostic path.\n"
            f"Supported local commands: {supported}"
        )
    if lowered == "/menu":
        return (
            "Simulated /menu\n"
            "Reply keyboard:\n"
            "- Дневник -> /menu\n"
            "- Статистика -> /status\n"
            "- Настройки -> /profile\n"
            "- Помощь -> /help"
        )
    if lowered == "/memory_stats":
        return (
            "Simulated /memory_stats\n"
            "This command is admin-gated. Run ./scripts/healbite check-admins to verify allow_admin_from "
            "and group_allow_admin_from before debugging routing."
        )
    scope = "7 days" if lowered.endswith("7d") else "today"
    if user_id is None:
        return (
            f"Simulated {normalized}\n"
            f"Local interpretation: nutrition diary summary for {scope}.\n"
            "No DB-backed user summary was rendered because --user-id was not provided."
        )
    return (
        f"Simulated {normalized}\n"
        f"Local interpretation: nutrition diary summary for user_id={user_id} over {scope}.\n"
        "Use --user-id to render a real local summary through the project diary formatter."
    )


def render_status_report(data: dict[str, Any]) -> str:
    git_status = data.get("git_status") or "(clean)"
    commits = data.get("recent_commits") or []
    runtime = data.get("runtime") or {}
    write_probe = data.get("write_probe") or {}
    lines = [
        "HealBite diagnostic status",
        "",
        "Git status:",
        git_status,
        "",
        "Recent commits:",
    ]
    if commits:
        lines.extend(f"- {item}" for item in commits)
    else:
        lines.append("- (no commits available)")
    lines.extend([
        "",
        "Container:",
        f"- name: {data.get('container_name', CONTAINER_NAME)}",
        f"- status: {data.get('container_status', 'unknown')}",
        f"- restart_count: {data.get('restart_count', 'unknown')}",
        "",
        "Runtime:",
        f"- hermes_home: {runtime.get('hermes_home', 'unknown')}",
        f"- config_path: {runtime.get('config_path', 'unknown')}",
        f"- env_path: {runtime.get('env_path', 'unknown')}",
        f"- model.provider: {runtime.get('model_provider', 'unknown')}",
        f"- model.default: {runtime.get('model_default', 'unknown')}",
        f"- auxiliary.vision.provider: {runtime.get('vision_provider', 'unknown')}",
        f"- auxiliary.vision.model: {runtime.get('vision_model', 'unknown')}",
        f"- check_vision_requirements: {runtime.get('vision_ready', 'unknown')}",
        f"- SQLite db_path: {runtime.get('db_path', 'unknown')}",
        f"- nutrition_log count: {runtime.get('nutrition_log_count', 'unknown')}",
        f"- admin-list loaded count: {runtime.get('admin_total_unique', 'unknown')}",
        "",
        "Presence flags:",
    ])
    for key, present in (runtime.get("secret_presence") or {}).items():
        lines.append(f"- {key}: {'yes' if present else 'no'}")
    qdrant_flags = runtime.get("qdrant_presence") or {}
    if qdrant_flags:
        lines.extend(["", "Qdrant env presence:"])
        for key, present in qdrant_flags.items():
            lines.append(f"- {key}: {'yes' if present else 'no'}")
    lines.extend([
        "",
        "Admin lists:",
        f"- allow_admin_from: {', '.join(runtime.get('allow_admin_from', [])) or '(empty)'}",
        f"- group_allow_admin_from: {', '.join(runtime.get('group_allow_admin_from', [])) or '(empty)'}",
        "",
        "Runtime DB write probe (uid=10000):",
        f"- ok: {write_probe.get('ok', False)}",
        f"- detail: {write_probe.get('detail', 'n/a')}",
    ])
    return redact_secrets("\n".join(lines))


class HealBiteCLI:
    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        runner: SubprocessRunner | None = None,
        container_name: str = CONTAINER_NAME,
    ) -> None:
        self.repo_root = repo_root or Path(__file__).resolve().parents[1]
        self.runner = runner or SubprocessRunner()
        self.container_name = container_name

    def _run_host(
        self, args: list[str], *, input_text: str | None = None, check: bool = True
    ) -> CommandResult:
        return self.runner.run(
            args, cwd=self.repo_root, input_text=input_text, check=check
        )

    def _docker_exec_python(
        self,
        code: str,
        *,
        user: str | None = None,
        check: bool = True,
    ) -> dict[str, Any]:
        cmd = ["docker", "exec", "-i"]
        if user:
            cmd.extend(["-u", user])
        cmd.extend([
            self.container_name,
            "sh",
            "-lc",
            (
                "cd /opt/hermes && "
                "if [ -x ./.venv/bin/python ]; then PY=./.venv/bin/python; "
                "elif [ -x ./venv/bin/python ]; then PY=./venv/bin/python; "
                "else PY=python3; fi; "
                'exec "$PY" -'
            ),
        ])
        result = self._run_host(cmd, input_text=code, check=check)
        payload_text = result.stdout.strip()
        if not payload_text:
            raise CLIError("Container returned no JSON payload.")
        try:
            return json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise CLIError(
                f"Could not parse container JSON: {redact_secrets(payload_text[:400])}"
            ) from exc

    def _container_state(self) -> tuple[str, int]:
        status = self._run_host([
            "docker",
            "inspect",
            "--format",
            "{{.State.Status}}",
            self.container_name,
        ]).stdout.strip()
        restart_raw = self._run_host([
            "docker",
            "inspect",
            "--format",
            "{{.RestartCount}}",
            self.container_name,
        ]).stdout.strip()
        try:
            restart_count = int(restart_raw)
        except ValueError:
            restart_count = -1
        return status, restart_count

    def cmd_status(self) -> str:
        git_status = self._run_host(
            ["git", "status", "--short"], check=False
        ).stdout.strip()
        commits_raw = self._run_host(
            ["git", "log", "--oneline", "-3"], check=False
        ).stdout.strip()
        status, restart_count = self._container_state()
        runtime = self._docker_exec_python(_RUNTIME_STATUS_CODE)
        write_probe = self._docker_exec_python(
            _RUNTIME_WRITE_PROBE_CODE, user="10000:10000"
        )
        data = {
            "git_status": git_status,
            "recent_commits": [
                line for line in commits_raw.splitlines() if line.strip()
            ],
            "container_name": self.container_name,
            "container_status": status,
            "restart_count": restart_count,
            "runtime": runtime,
            "write_probe": write_probe,
        }
        return render_status_report(data)

    def cmd_logs(self, last: int) -> str:
        raw = self._run_host(
            ["docker", "logs", "--tail", str(last), self.container_name],
            check=False,
        )
        if raw.returncode != 0:
            raise CLIError(
                redact_secrets(
                    (raw.stderr or raw.stdout).strip() or "docker logs failed"
                )
            )
        combined = "\n".join(part for part in [raw.stdout, raw.stderr] if part)
        filtered = filter_log_lines(combined)
        if not filtered:
            return f"No matching diagnostic log lines found in the last {last} lines."
        return "\n".join(filtered)

    def cmd_test_diary(self) -> str:
        result = self._docker_exec_python(_TEST_DIARY_CODE, user="10000:10000")
        lines = [
            "HealBite diary diagnostic",
            f"- db_path: {result.get('db_path', 'unknown')}",
            f"- schema_ok: {result.get('schema_ok', False)}",
            f"- nutrition_log_count: {result.get('nutrition_log_count', 'unknown')}",
            f"- write_probe_ok: {result.get('write_probe_ok', False)}",
            f"- summary_ok: {result.get('summary_ok', False)}",
            f"- encoding_ok: {result.get('encoding_ok', False)}",
        ]
        columns = result.get("columns") or []
        if columns:
            lines.append(f"- columns: {', '.join(columns)}")
        preview = result.get("summary_preview")
        if preview:
            lines.extend(["", "Probe summary preview:", preview])
        detail = result.get("detail")
        if detail:
            lines.append(f"- detail: {detail}")
        return redact_secrets("\n".join(lines))

    def cmd_check_admins(self) -> str:
        result = self._docker_exec_python(_CHECK_ADMINS_CODE)
        lines = [
            "HealBite admin policy diagnostic",
            f"- allow_admin_from: {', '.join(result.get('allow_admin_from', [])) or '(empty)'}",
            f"- group_allow_admin_from: {', '.join(result.get('group_allow_admin_from', [])) or '(empty)'}",
            "",
            "Known IDs:",
        ]
        for item in result.get("known_ids", []):
            lines.append(
                f"- {item['user_id']}: dm_admin={item['dm_admin']} group_admin={item['group_admin']}"
            )
        return "\n".join(lines)

    def cmd_inspect_profile(self, user_id: int) -> str:
        result = self._docker_exec_python(
            _INSPECT_PROFILE_CODE_TEMPLATE.format(user_id=int(user_id))
        )
        lines = [
            f"HealBite profile diagnostic for user_id={user_id}",
            f"- db_path: {result.get('db_path', 'unknown')}",
            f"- profile_found: {result.get('profile_found', False)}",
        ]
        profile = result.get("profile") or {}
        if profile:
            lines.append("- sanitized profile fields:")
            for key, value in profile.items():
                lines.append(f"  - {key}: {value}")
        targets = {
            key: value
            for key, value in (result.get("nutrition_targets") or {}).items()
            if value is not None
        }
        if targets:
            lines.append("- nutrition targets:")
            for key, value in targets.items():
                lines.append(f"  - {key}: {value}")
        detail = result.get("detail")
        if detail:
            lines.append(f"- detail: {detail}")
        return redact_secrets("\n".join(lines))

    def cmd_simulate_message(self, text: str, *, user_id: int | None = None) -> str:
        lowered = normalize_simulation_text(text).lower()
        if user_id is None or lowered not in {
            "/diary",
            "/diary 7d",
            "/stats",
            "/stats 7d",
        }:
            return simulate_local_message(text, user_id=user_id)
        result = self._docker_exec_python(
            _SIMULATE_DIARY_CODE_TEMPLATE.format(
                user_id=int(user_id),
                days=7 if lowered.endswith("7d") else 1,
            )
        )
        if not result.get("ok", False):
            detail = result.get("detail", "Local diary simulation failed.")
            return f"Simulated {normalize_simulation_text(text)}\n{detail}"
        return (
            f"Simulated {normalize_simulation_text(text)} for user_id={user_id}\n"
            f"{result.get('text', '').strip()}"
        )

    def cmd_fix_plan(self, issue: str) -> str:
        return build_fix_plan(issue)


_RUNTIME_STATUS_CODE = r"""
from __future__ import annotations
import json
import os
import sqlite3
from pathlib import Path

import yaml

from gateway.config import Platform, load_gateway_config
from gateway.healbite_nutrition_diary import NUTRITION_LOG_TABLE, resolve_healbite_db_path
from gateway.slash_access import policy_from_extra
from hermes_cli.config import get_config_path, get_hermes_home
from tools.vision_tools import check_vision_requirements

home = get_hermes_home()
config_path = Path(get_config_path())
env_path = home / ".env"
raw = {}
if config_path.exists():
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

model_cfg = raw.get("model") if isinstance(raw.get("model"), dict) else {}
aux_cfg = raw.get("auxiliary") if isinstance(raw.get("auxiliary"), dict) else {}
vision_cfg = aux_cfg.get("vision") if isinstance(aux_cfg.get("vision"), dict) else {}

config = load_gateway_config()
telegram_cfg = config.platforms.get(Platform.TELEGRAM)
extra = getattr(telegram_cfg, "extra", {}) if telegram_cfg else {}
dm_policy = policy_from_extra(extra, "dm")
group_policy = policy_from_extra(extra, "group")

db_path = resolve_healbite_db_path()
nutrition_count = None
if db_path.exists():
    try:
        with sqlite3.connect(db_path) as conn:
            nutrition_count = conn.execute(
                f"SELECT COUNT(*) FROM {NUTRITION_LOG_TABLE}"
            ).fetchone()[0]
    except Exception as exc:
        nutrition_count = f"error:{type(exc).__name__}"

secret_presence = {
    "GEMINI_API_KEY": bool(os.getenv("GEMINI_API_KEY")),
    "DEEPSEEK_API_KEY": bool(os.getenv("DEEPSEEK_API_KEY")),
    "TELEGRAM_BOT_TOKEN": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
}
qdrant_presence = {
    "QDRANT_URL": bool(os.getenv("QDRANT_URL")),
    "QDRANT_API_KEY": bool(os.getenv("QDRANT_API_KEY")),
    "QDRANT_HOST": bool(os.getenv("QDRANT_HOST")),
    "QDRANT_PORT": bool(os.getenv("QDRANT_PORT")),
}

admin_ids = set(dm_policy.admin_user_ids) | set(group_policy.admin_user_ids)

payload = {
    "hermes_home": str(home),
    "config_path": str(config_path),
    "env_path": str(env_path),
    "model_provider": str(model_cfg.get("provider") or "unknown"),
    "model_default": str(model_cfg.get("default") or model_cfg.get("model") or "unknown"),
    "vision_provider": str(vision_cfg.get("provider") or "unknown"),
    "vision_model": str(vision_cfg.get("model") or "unknown"),
    "vision_ready": bool(check_vision_requirements()),
    "secret_presence": secret_presence,
    "qdrant_presence": qdrant_presence,
    "db_path": str(db_path),
    "nutrition_log_count": nutrition_count,
    "allow_admin_from": sorted(dm_policy.admin_user_ids),
    "group_allow_admin_from": sorted(group_policy.admin_user_ids),
    "admin_total_unique": len(admin_ids),
}
print(json.dumps(payload, ensure_ascii=False))
"""


_RUNTIME_WRITE_PROBE_CODE = r'''
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone

from gateway.healbite_nutrition_diary import NUTRITION_LOG_TABLE, resolve_healbite_db_path

db_path = resolve_healbite_db_path()
probe_user_id = 990000001
probe_ref = "healbite-status-probe"
payload = {"ok": False, "detail": "not started"}

try:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({NUTRITION_LOG_TABLE})").fetchall()
        }
        if not columns:
            raise RuntimeError("nutrition_log schema is missing")
        cursor = conn.execute(
            f"""
            INSERT INTO {NUTRITION_LOG_TABLE}(
                user_id, source, meal_name, items_json, calories_kcal, protein_g,
                fat_g, carbs_g, confidence, occurred_at, raw_summary, image_ref, qdrant_indexed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                probe_user_id,
                "probe",
                "CLI probe",
                "[]",
                123.0,
                10.0,
                4.0,
                11.0,
                1.0,
                now,
                "CLI write probe",
                probe_ref,
                0,
            ),
        )
        probe_id = int(cursor.lastrowid)
        conn.commit()
        conn.execute(
            f"DELETE FROM {NUTRITION_LOG_TABLE} WHERE id = ? AND user_id = ?",
            (probe_id, probe_user_id),
        )
        conn.commit()
    payload = {"ok": True, "detail": "insert/delete probe succeeded"}
except Exception as exc:
    payload = {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

print(json.dumps(payload, ensure_ascii=False))
'''


_TEST_DIARY_CODE = r'''
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone

from gateway.healbite_nutrition_diary import (
    NUTRITION_LOG_TABLE,
    compute_nutrition_diary_summary,
    format_nutrition_diary_report,
    resolve_healbite_db_path,
)

db_path = resolve_healbite_db_path()
probe_user_id = 990000002
probe_ref = "healbite-diary-probe"
result = {
    "db_path": str(db_path),
    "schema_ok": False,
    "nutrition_log_count": None,
    "write_probe_ok": False,
    "summary_ok": False,
    "encoding_ok": False,
    "columns": [],
    "summary_preview": "",
    "detail": "",
}

try:
    with sqlite3.connect(db_path) as conn:
        columns = [
            row[1]
            for row in conn.execute(f"PRAGMA table_info({NUTRITION_LOG_TABLE})").fetchall()
        ]
        result["columns"] = columns
        result["schema_ok"] = bool(columns)
        if not columns:
            raise RuntimeError("nutrition_log schema is missing")
        result["nutrition_log_count"] = conn.execute(
            f"SELECT COUNT(*) FROM {NUTRITION_LOG_TABLE}"
        ).fetchone()[0]
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = conn.execute(
            f"""
            INSERT INTO {NUTRITION_LOG_TABLE}(
                user_id, source, meal_name, items_json, calories_kcal, protein_g,
                fat_g, carbs_g, confidence, occurred_at, raw_summary, image_ref, qdrant_indexed
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                probe_user_id,
                "probe",
                "CLI probe meal",
                '[{"name":"probe meal"}]',
                321.0,
                22.0,
                9.0,
                31.0,
                1.0,
                now,
                "CLI diary probe",
                probe_ref,
                0,
            ),
        )
        probe_id = int(cursor.lastrowid)
        conn.commit()
        result["write_probe_ok"] = True
        summary = compute_nutrition_diary_summary(db_path=db_path, user_id=probe_user_id, days=1)
        report = format_nutrition_diary_report(summary)
        result["summary_ok"] = bool(summary.get("entry_count"))
        result["encoding_ok"] = "????" not in report
        result["summary_preview"] = "\n".join(report.splitlines()[:8])
        conn.execute(
            f"DELETE FROM {NUTRITION_LOG_TABLE} WHERE id = ? AND user_id = ?",
            (probe_id, probe_user_id),
        )
        conn.commit()
except Exception as exc:
    result["detail"] = f"{type(exc).__name__}: {exc}"

print(json.dumps(result, ensure_ascii=False))
'''


_CHECK_ADMINS_CODE = r"""
from __future__ import annotations
import json
from types import SimpleNamespace

from gateway.config import Platform, load_gateway_config
from gateway.slash_access import policy_for_source

known_ids = ["968323641", "248875361", "5179574383"]
config = load_gateway_config()
telegram_cfg = config.platforms.get(Platform.TELEGRAM)
extra = getattr(telegram_cfg, "extra", {}) if telegram_cfg else {}
result = {
    "allow_admin_from": [str(v) for v in extra.get("allow_admin_from", [])],
    "group_allow_admin_from": [str(v) for v in extra.get("group_allow_admin_from", [])],
    "known_ids": [],
}
for user_id in known_ids:
    dm_source = SimpleNamespace(platform=Platform.TELEGRAM, chat_type="private", user_id=user_id)
    group_source = SimpleNamespace(platform=Platform.TELEGRAM, chat_type="group", user_id=user_id)
    dm_policy = policy_for_source(config, dm_source)
    group_policy = policy_for_source(config, group_source)
    result["known_ids"].append(
        {
            "user_id": user_id,
            "dm_admin": dm_policy.is_admin(user_id),
            "group_admin": group_policy.is_admin(user_id),
        }
    )
print(json.dumps(result, ensure_ascii=False))
"""


_INSPECT_PROFILE_CODE_TEMPLATE = r"""
from __future__ import annotations
import json
import sqlite3

from gateway.healbite_nutrition_diary import _load_nutrition_targets, resolve_healbite_db_path

user_id = {user_id}
db_path = resolve_healbite_db_path()
result = {{
    "db_path": str(db_path),
    "profile_found": False,
    "profile": {{}},
    "nutrition_targets": {{}},
    "detail": "",
}}

try:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {{
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }}
        if "profiles" not in tables:
            raise RuntimeError("profiles table is missing")
        columns = {{
            row[1]
            for row in conn.execute("PRAGMA table_info(profiles)").fetchall()
        }}
        identity_column = None
        for candidate in ("telegram_id", "user_id", "id"):
            if candidate in columns:
                identity_column = candidate
                break
        if identity_column is None:
            raise RuntimeError("profiles table has no supported identity column")
        row = conn.execute(
            f"SELECT * FROM profiles WHERE {{identity_column}} = ? LIMIT 1",
            (user_id,),
        ).fetchone()
        result["profile_found"] = row is not None
        if row is not None:
            wanted = (
                "goal",
                "activity",
                "activity_level",
                "calories_limit",
                "timezone",
                "onboarding_done",
            )
            result["profile"] = {{
                key: row[key]
                for key in wanted
                if key in row.keys() and row[key] not in (None, "")
            }}
        targets = _load_nutrition_targets(conn, user_id=user_id)
        result["nutrition_targets"] = {{
            "calories_kcal": targets.calories_kcal,
            "protein_g": targets.protein_g,
            "fat_g": targets.fat_g,
            "carbs_g": targets.carbs_g,
        }}
except Exception as exc:
    result["detail"] = f"{{type(exc).__name__}}: {{exc}}"

print(json.dumps(result, ensure_ascii=False))
"""


_SIMULATE_DIARY_CODE_TEMPLATE = r"""
from __future__ import annotations
import json

from gateway.healbite_nutrition_diary import compute_nutrition_diary_summary, format_nutrition_diary_report, resolve_healbite_db_path

user_id = {user_id}
days = {days}
result = {{"ok": False, "text": "", "detail": ""}}
try:
    summary = compute_nutrition_diary_summary(db_path=resolve_healbite_db_path(), user_id=user_id, days=days)
    text = format_nutrition_diary_report(summary)
    result["ok"] = True
    result["text"] = text
except Exception as exc:
    result["detail"] = f"{{type(exc).__name__}}: {{exc}}"

print(json.dumps(result, ensure_ascii=False))
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HealBite diagnostic CLI for agent workflows"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "status", help="Inspect git, container runtime, and DB health"
    )

    logs_parser = subparsers.add_parser("logs", help="Show filtered hermes-bot logs")
    logs_parser.add_argument(
        "--last", type=int, default=DEFAULT_LOG_TAIL, help="Tail size for docker logs"
    )

    subparsers.add_parser(
        "test-diary", help="Probe nutrition_log and diary formatter safely"
    )
    subparsers.add_parser("check-admins", help="Inspect effective admin ACL policy")

    inspect_parser = subparsers.add_parser(
        "inspect-profile", help="Show a sanitized user profile"
    )
    inspect_parser.add_argument(
        "--user-id", type=int, required=True, help="Telegram user ID"
    )

    simulate_parser = subparsers.add_parser(
        "simulate-message", help="Dry-run supported local HealBite commands"
    )
    simulate_parser.add_argument("text", help='Message text, e.g. "/diary 7d"')
    simulate_parser.add_argument(
        "--user-id",
        type=int,
        default=None,
        help="Optional user for diary/stats summary",
    )

    fix_parser = subparsers.add_parser(
        "fix-plan", help="Show a diagnostic plan for a known issue"
    )
    fix_parser.add_argument(
        "--issue", required=True, choices=sorted(FIX_PLANS), help="Issue slug"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cli = HealBiteCLI()
    if args.command == "status":
        print(cli.cmd_status())
        return 0
    if args.command == "logs":
        print(cli.cmd_logs(args.last))
        return 0
    if args.command == "test-diary":
        print(cli.cmd_test_diary())
        return 0
    if args.command == "check-admins":
        print(cli.cmd_check_admins())
        return 0
    if args.command == "inspect-profile":
        print(cli.cmd_inspect_profile(args.user_id))
        return 0
    if args.command == "simulate-message":
        print(cli.cmd_simulate_message(args.text, user_id=args.user_id))
        return 0
    if args.command == "fix-plan":
        print(cli.cmd_fix_plan(args.issue))
        return 0
    raise CLIError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CLIError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
