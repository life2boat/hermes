#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.healbite_nutrition_diary import (
    HealBiteNutritionDiary,
    NUTRITION_LOG_TABLE,
    PENDING_MEALS_TABLE,
    compute_nutrition_diary_summary,
    format_pending_meal_cancelled_reply,
    format_pending_meal_expired_reply,
    format_pending_meal_saved_reply,
    format_pending_meal_wait_reply,
    format_nutrition_diary_report,
    normalize_nutrition_payload,
    resolve_healbite_db_path,
)
from gateway.healbite_user_profile import (
    HealBiteUserProfileStore,
    format_healbite_profile_report,
)

CONTAINER_NAME = "hermes-bot"
DEFAULT_LOG_TAIL = 80
SUPPORTED_SIMULATION_COMMANDS = {
    "/diary",
    "/diary 7d",
    "/stats",
    "/stats 7d",
    "/undo_meal",
    "/diary_undo",
    "/memory_stats",
    "/menu",
    "/profile",
}
STATE_CHANGING_SIMULATION_COMMANDS = {
    "/undo_meal",
    "/diary_undo",
}
READ_ONLY_DIARY_PHRASES = {
    "\u0447\u0442\u043e \u0443 \u043c\u0435\u043d\u044f \u0441\u0435\u0433\u043e\u0434\u043d\u044f \u0432 \u0434\u043d\u0435\u0432\u043d\u0438\u043a\u0435?",
    "\u0447\u0442\u043e \u0443 \u043c\u0435\u043d\u044f \u0441\u0435\u0433\u043e\u0434\u043d\u044f \u0432 \u0434\u043d\u0435\u0432\u043d\u0438\u043a\u0435",
}
AMBIGUOUS_DIARY_PHRASES = {
    "\u043d\u0430\u0432\u0435\u0440\u043d\u043e\u0435 \u0442\u0443\u0442 \u043e\u0448\u0438\u0431\u043a\u0430",
    "\u0438\u0441\u043f\u0440\u0430\u0432\u044c \u043e\u0448\u0438\u0431\u043a\u0443",
}
CORRECTION_SMOKE_USER_ID = 999999
CORRECTION_SMOKE_SOURCE = "cli_correction_smoke"
CORRECTION_SMOKE_IMAGE_REF_PREFIX = "cli-correction-smoke-"
PENDING_SMOKE_USER_ID = 999999
PENDING_SMOKE_SOURCE = "cli_pending_smoke"
PENDING_SMOKE_IMAGE_REF_PREFIX = "cli-pending-smoke-"
PROFILE_SMOKE_USER_ID = 999998
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


@dataclass(frozen=True)
class DiaryCorrectionIntent:
    kind: str
    value: float | str


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


def parse_diary_correction_intent(text: str) -> DiaryCorrectionIntent | None:
    normalized = normalize_simulation_text(text)
    set_match = re.fullmatch(
        r"\u0438\u0441\u043f\u0440\u0430\u0432\u044c \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e \u0437\u0430\u043f\u0438\u0441\u044c \u043d\u0430 (\d+(?:[.,]\d+)?) \u043a\u043a\u0430\u043b",
        normalized,
        flags=re.IGNORECASE,
    )
    if set_match:
        return DiaryCorrectionIntent(
            "set_calories", float(set_match.group(1).replace(",", "."))
        )

    delta_match = re.fullmatch(
        r"\u0434\u043e\u0431\u0430\u0432\u044c \u043a \u043f\u043e\u0441\u043b\u0435\u0434\u043d(?:\u0435\u0439)? \u0437\u0430\u043f\u0438\u0441\u0438 (\d+(?:[.,]\d+)?) \u043a\u043a\u0430\u043b",
        normalized,
        flags=re.IGNORECASE,
    )
    if delta_match:
        return DiaryCorrectionIntent(
            "add_calories", float(delta_match.group(1).replace(",", "."))
        )

    rename_match = re.fullmatch(
        r"\u043f\u0435\u0440\u0435\u0438\u043c\u0435\u043d\u0443\u0439 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e \u0437\u0430\u043f\u0438\u0441\u044c \u0432 (.+)",
        normalized,
        flags=re.IGNORECASE,
    )
    if rename_match:
        meal_name = rename_match.group(1).strip()
        if meal_name:
            return DiaryCorrectionIntent("rename", meal_name)
    return None


def _format_simulated_metric(value: float | int | None, unit: str) -> str | None:
    if value is None:
        return None
    numeric = float(value)
    if numeric.is_integer():
        rendered = str(int(numeric))
    else:
        rendered = f"{numeric:.1f}".rstrip("0").rstrip(".")
    return f"{rendered} {unit}"


def _build_simulated_update_reply(
    *,
    meal_name: str | None,
    calories_kcal: float | int | None,
    protein_g: float | int | None,
    fat_g: float | int | None,
    carbs_g: float | int | None,
) -> str:
    title = (meal_name or "\u041f\u043e\u0441\u043b\u0435\u0434\u043d\u044f\u044f \u0437\u0430\u043f\u0438\u0441\u044c").strip()
    calories = _format_simulated_metric(calories_kcal, "\u043a\u043a\u0430\u043b") or "\u0431\u0435\u0437 \u043a\u0430\u043b\u043e\u0440\u0438\u0439"
    macro_values = {
        "\u0411": _format_simulated_metric(protein_g, "\u0433"),
        "\u0416": _format_simulated_metric(fat_g, "\u0433"),
        "\u0423": _format_simulated_metric(carbs_g, "\u0433"),
    }
    macro_parts = [
        f"{label} {value}" for label, value in macro_values.items() if value is not None
    ]
    macros_line = ""
    if macro_parts:
        macros_line = "\n" + " \u00b7 ".join(macro_parts)
    return (
        "\u2705 \u0418\u0441\u043f\u0440\u0430\u0432\u0438\u043b \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e \u0437\u0430\u043f\u0438\u0441\u044c:\n"
        f"{title} \u2014 {calories}"
        f"{macros_line}\n\n"
        "\u0412\u044b\u0437\u043e\u0432\u0438 /diary, \u0447\u0442\u043e\u0431\u044b \u043f\u043e\u0441\u043c\u043e\u0442\u0440\u0435\u0442\u044c \u0438\u0442\u043e\u0433 \u0437\u0430 \u0434\u0435\u043d\u044c."
    )


def _render_local_diary_report(
    *,
    user_id: int,
    days: int = 1,
    db_path: str | Path | None = None,
) -> str:
    summary = compute_nutrition_diary_summary(
        db_path=resolve_healbite_db_path(db_path),
        user_id=int(user_id),
        days=max(1, int(days)),
    )
    return format_nutrition_diary_report(summary)


def _render_diary_clarification() -> str:
    return (
        "\u041d\u0443\u0436\u043d\u043e \u043a\u043e\u043d\u043a\u0440\u0435\u0442\u043d\u043e\u0435 \u0438\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435: "
        "\u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440, \u00ab\u0438\u0441\u043f\u0440\u0430\u0432\u044c \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e "
        "\u0437\u0430\u043f\u0438\u0441\u044c \u043d\u0430 400 \u043a\u043a\u0430\u043b\u00bb \u0438\u043b\u0438 "
        "\u00ab\u043f\u0435\u0440\u0435\u0438\u043c\u0435\u043d\u0443\u0439 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e \u0437\u0430\u043f\u0438\u0441\u044c \u0432 \u0431\u043e\u0440\u0449\u00bb."
    )


def _read_only_write_guard(intent: DiaryCorrectionIntent) -> str:
    if intent.kind == "set_calories":
        detail = (
            "\u0438\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u043a\u0430\u043b\u043e\u0440\u0438\u0438 \u043d\u0430 "
            f"{int(intent.value) if float(intent.value).is_integer() else intent.value} \u043a\u043a\u0430\u043b"
        )
    elif intent.kind == "add_calories":
        detail = (
            "\u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c "
            f"{int(intent.value) if float(intent.value).is_integer() else intent.value} \u043a\u043a\u0430\u043b "
            "\u043a \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0439 \u0437\u0430\u043f\u0438\u0441\u0438"
        )
    else:
        detail = (
            "\u043f\u0435\u0440\u0435\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u0442\u044c \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e "
            f"\u0437\u0430\u043f\u0438\u0441\u044c \u0432 \u00ab{intent.value}\u00bb"
        )
    return (
        "This command changes state. Use --allow-write to execute.\n"
        f"\u0420\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u043d\u043e \u043d\u0430\u043c\u0435\u0440\u0435\u043d\u0438\u0435: {detail}."
    )


def _apply_local_correction(
    *,
    intent: DiaryCorrectionIntent,
    user_id: int,
    db_path: str | Path | None = None,
) -> str:
    diary = HealBiteNutritionDiary(
        db_path=resolve_healbite_db_path(db_path), background_write=False
    )
    if intent.kind == "set_calories":
        result = diary.update_last_meal(
            user_id=str(user_id),
            new_calories=int(round(float(intent.value))),
        )
    elif intent.kind == "rename":
        result = diary.update_last_meal(
            user_id=str(user_id),
            new_meal_name=str(intent.value),
        )
    else:
        summary = diary.get_daily_summary(user_id=int(user_id))
        entries = summary.get("entries") or []
        if not entries:
            return (
                "\u0421\u0435\u0433\u043e\u0434\u043d\u044f \u0432 \u0434\u043d\u0435\u0432\u043d\u0438\u043a\u0435 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442 "
                "\u0437\u0430\u043f\u0438\u0441\u0435\u0439 \u0434\u043b\u044f \u0438\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f."
            )
        current_calories = float(entries[-1].get("calories_kcal") or 0.0)
        result = diary.update_last_meal(
            user_id=str(user_id),
            new_calories=int(round(current_calories + float(intent.value))),
        )
    if not result.updated:
        return (
            "\u0421\u0435\u0433\u043e\u0434\u043d\u044f \u0432 \u0434\u043d\u0435\u0432\u043d\u0438\u043a\u0435 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442 "
            "\u0437\u0430\u043f\u0438\u0441\u0435\u0439 \u0434\u043b\u044f \u0438\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f."
        )
    return _build_simulated_update_reply(
        meal_name=result.meal_name,
        calories_kcal=result.calories_kcal,
        protein_g=result.protein_g,
        fat_g=result.fat_g,
        carbs_g=result.carbs_g,
    )


def _count_synthetic_correction_rows(
    *,
    db_path: str | Path | None = None,
    user_id: int = CORRECTION_SMOKE_USER_ID,
    source: str = CORRECTION_SMOKE_SOURCE,
) -> int:
    resolved = resolve_healbite_db_path(db_path)
    if not Path(resolved).exists():
        return 0
    with sqlite3.connect(resolved) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (NUTRITION_LOG_TABLE,),
        ).fetchone()
        if row is None:
            return 0
        row = conn.execute(
            f"SELECT COUNT(*) FROM {NUTRITION_LOG_TABLE} WHERE user_id = ? AND source = ?",
            (int(user_id), source),
        ).fetchone()
    return int(row[0] if row else 0)


def _cleanup_synthetic_correction_rows(
    *,
    db_path: str | Path | None = None,
    user_id: int = CORRECTION_SMOKE_USER_ID,
    source: str = CORRECTION_SMOKE_SOURCE,
) -> int:
    resolved = resolve_healbite_db_path(db_path)
    if not Path(resolved).exists():
        return 0
    with sqlite3.connect(resolved) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (NUTRITION_LOG_TABLE,),
        ).fetchone()
        if row is None:
            return 0
        conn.execute(
            f"DELETE FROM {NUTRITION_LOG_TABLE} WHERE user_id = ? AND source = ?",
            (int(user_id), source),
        )
        conn.commit()
    return _count_synthetic_correction_rows(db_path=resolved, user_id=user_id, source=source)


def _seed_synthetic_correction_row(
    *,
    db_path: str | Path | None = None,
    user_id: int = CORRECTION_SMOKE_USER_ID,
    source: str = CORRECTION_SMOKE_SOURCE,
) -> str:
    resolved = resolve_healbite_db_path(db_path)
    diary = HealBiteNutritionDiary(db_path=resolved, background_write=False)
    image_ref = f"{CORRECTION_SMOKE_IMAGE_REF_PREFIX}{time.time_ns()}"
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "CLI smoke meal",
                "raw_summary": "CLI smoke meal summary",
                "confidence": 0.9,
                "totals": {
                    "calories_kcal": 321,
                    "protein_g": 22,
                    "fat_g": 9,
                    "carbs_g": 31,
                },
                "items": [{"name": "CLI smoke meal"}],
            },
            ensure_ascii=False,
        )
    )
    diary.save_record(
        user_id=int(user_id),
        source=source,
        record=record,
        image_ref=image_ref,
        occurred_at=None,
    )
    return image_ref


def _latest_entry_for_user(
    *,
    db_path: str | Path | None = None,
    user_id: int,
) -> dict[str, Any]:
    summary = compute_nutrition_diary_summary(
        db_path=resolve_healbite_db_path(db_path),
        user_id=int(user_id),
        days=1,
    )
    entries = summary.get("entries") or []
    if not entries:
        raise CLIError("Synthetic correction smoke expected a diary entry but found none.")
    return dict(entries[-1])


def _maybe_fail_correction_smoke(fail_after_step: str | None, current_step: str) -> None:
    if fail_after_step and fail_after_step == current_step:
        raise RuntimeError(f"Injected failure after step: {current_step}")


def run_local_correction_smoke(
    *,
    db_path: str | Path | None = None,
    user_id: int = CORRECTION_SMOKE_USER_ID,
    source: str = CORRECTION_SMOKE_SOURCE,
    fail_after_step: str | None = None,
) -> list[str]:
    resolved = resolve_healbite_db_path(db_path)
    markers: list[str] = []
    pending_error: Exception | None = None

    _cleanup_synthetic_correction_rows(db_path=resolved, user_id=user_id, source=source)
    _seed_synthetic_correction_row(db_path=resolved, user_id=user_id, source=source)
    try:
        guard_report = simulate_local_message(
            "\u0438\u0441\u043f\u0440\u0430\u0432\u044c \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e \u0437\u0430\u043f\u0438\u0441\u044c \u043d\u0430 400 \u043a\u043a\u0430\u043b",
            user_id=user_id,
            allow_write=False,
            db_path=resolved,
        )
        guard_entry = _latest_entry_for_user(db_path=resolved, user_id=user_id)
        if "Use --allow-write" not in guard_report or float(guard_entry.get("calories_kcal") or 0.0) != 321.0:
            raise CLIError("Correction guard smoke failed.")
        markers.append("correction_guard_ok")
        _maybe_fail_correction_smoke(fail_after_step, "guard")

        set_report = simulate_local_message(
            "\u0438\u0441\u043f\u0440\u0430\u0432\u044c \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e \u0437\u0430\u043f\u0438\u0441\u044c \u043d\u0430 400 \u043a\u043a\u0430\u043b",
            user_id=user_id,
            allow_write=True,
            db_path=resolved,
        )
        set_entry = _latest_entry_for_user(db_path=resolved, user_id=user_id)
        if not set_report.startswith("\u2705 \u0418\u0441\u043f\u0440\u0430\u0432\u0438\u043b \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e \u0437\u0430\u043f\u0438\u0441\u044c:") or float(set_entry.get("calories_kcal") or 0.0) != 400.0:
            raise CLIError("Set calories smoke failed.")
        markers.append("set_calories_ok")
        _maybe_fail_correction_smoke(fail_after_step, "set")

        add_report = simulate_local_message(
            "\u0434\u043e\u0431\u0430\u0432\u044c \u043a \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0439 \u0437\u0430\u043f\u0438\u0441\u0438 100 \u043a\u043a\u0430\u043b",
            user_id=user_id,
            allow_write=True,
            db_path=resolved,
        )
        add_entry = _latest_entry_for_user(db_path=resolved, user_id=user_id)
        if "\u2014 500 \u043a\u043a\u0430\u043b" not in add_report or float(add_entry.get("calories_kcal") or 0.0) != 500.0:
            raise CLIError("Add calories smoke failed.")
        markers.append("add_calories_ok")
        _maybe_fail_correction_smoke(fail_after_step, "add")

        rename_report = simulate_local_message(
            "\u043f\u0435\u0440\u0435\u0438\u043c\u0435\u043d\u0443\u0439 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044e\u044e \u0437\u0430\u043f\u0438\u0441\u044c \u0432 \u0431\u043e\u0440\u0449",
            user_id=user_id,
            allow_write=True,
            db_path=resolved,
        )
        rename_entry = _latest_entry_for_user(db_path=resolved, user_id=user_id)
        if "\u0431\u043e\u0440\u0449" not in rename_report or str(rename_entry.get("meal_name") or "") != "\u0431\u043e\u0440\u0449":
            raise CLIError("Rename smoke failed.")
        markers.append("rename_ok")
        _maybe_fail_correction_smoke(fail_after_step, "rename")

        read_only_before = dict(rename_entry)
        read_only_report = simulate_local_message(
            "\u0447\u0442\u043e \u0443 \u043c\u0435\u043d\u044f \u0441\u0435\u0433\u043e\u0434\u043d\u044f \u0432 \u0434\u043d\u0435\u0432\u043d\u0438\u043a\u0435?",
            user_id=user_id,
            allow_write=False,
            db_path=resolved,
        )
        read_only_after = _latest_entry_for_user(db_path=resolved, user_id=user_id)
        if "\u0422\u0432\u043e\u0439 \u0434\u043d\u0435\u0432\u043d\u0438\u043a \u0437\u0430 \u0441\u0435\u0433\u043e\u0434\u043d\u044f" not in read_only_report or "\u0431\u043e\u0440\u0449" not in read_only_report or read_only_before != read_only_after:
            raise CLIError("Read-only correction smoke failed.")
        markers.append("read_only_ok")
        _maybe_fail_correction_smoke(fail_after_step, "read_only")

        ambiguous_before = dict(read_only_after)
        ambiguous_report = simulate_local_message(
            "\u0438\u0441\u043f\u0440\u0430\u0432\u044c \u043e\u0448\u0438\u0431\u043a\u0443",
            user_id=user_id,
            allow_write=False,
            db_path=resolved,
        )
        ambiguous_after = _latest_entry_for_user(db_path=resolved, user_id=user_id)
        if "\u041d\u0443\u0436\u043d\u043e \u043a\u043e\u043d\u043a\u0440\u0435\u0442\u043d\u043e\u0435 \u0438\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435" not in ambiguous_report or ambiguous_before != ambiguous_after:
            raise CLIError("Ambiguous correction smoke failed.")
        markers.append("ambiguous_noop_ok")
    except Exception as exc:
        pending_error = exc
    finally:
        remaining = _cleanup_synthetic_correction_rows(
            db_path=resolved,
            user_id=user_id,
            source=source,
        )
        if remaining != 0:
            raise CLIError("Synthetic correction cleanup failed.") from pending_error
        markers.append("cleanup_ok")

    if pending_error is not None:
        raise pending_error
    return markers


def _count_pending_rows(
    *,
    db_path: str | Path | None = None,
    user_id: int = PENDING_SMOKE_USER_ID,
) -> int:
    resolved = resolve_healbite_db_path(db_path)
    if not Path(resolved).exists():
        return 0
    with sqlite3.connect(resolved) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (PENDING_MEALS_TABLE,),
        ).fetchone()
        if row is None:
            return 0
        row = conn.execute(
            f"SELECT COUNT(*) FROM {PENDING_MEALS_TABLE} WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    return int(row[0] if row else 0)


def _count_synthetic_pending_nutrition_rows(
    *,
    db_path: str | Path | None = None,
    user_id: int = PENDING_SMOKE_USER_ID,
    source: str = PENDING_SMOKE_SOURCE,
) -> int:
    resolved = resolve_healbite_db_path(db_path)
    if not Path(resolved).exists():
        return 0
    with sqlite3.connect(resolved) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (NUTRITION_LOG_TABLE,),
        ).fetchone()
        if row is None:
            return 0
        row = conn.execute(
            f"SELECT COUNT(*) FROM {NUTRITION_LOG_TABLE} WHERE user_id = ? AND source = ?",
            (int(user_id), source),
        ).fetchone()
    return int(row[0] if row else 0)


def _cleanup_synthetic_pending_state(
    *,
    db_path: str | Path | None = None,
    user_id: int = PENDING_SMOKE_USER_ID,
    source: str = PENDING_SMOKE_SOURCE,
) -> tuple[int, int]:
    resolved = resolve_healbite_db_path(db_path)
    if Path(resolved).exists():
        with sqlite3.connect(resolved) as conn:
            conn.execute(
                f"DELETE FROM {PENDING_MEALS_TABLE} WHERE user_id = ?",
                (int(user_id),),
            )
            conn.execute(
                f"DELETE FROM {NUTRITION_LOG_TABLE} WHERE user_id = ? AND source = ?",
                (int(user_id), source),
            )
            conn.commit()
    return (
        _count_pending_rows(db_path=resolved, user_id=user_id),
        _count_synthetic_pending_nutrition_rows(
            db_path=resolved,
            user_id=user_id,
            source=source,
        ),
    )


def _seed_synthetic_pending_row(
    *,
    db_path: str | Path | None = None,
    user_id: int = PENDING_SMOKE_USER_ID,
    source: str = PENDING_SMOKE_SOURCE,
    now: datetime | None = None,
    expired: bool = False,
) -> str:
    resolved = resolve_healbite_db_path(db_path)
    diary = HealBiteNutritionDiary(db_path=resolved, background_write=False)
    base_now = now or datetime.now(timezone.utc)
    image_ref = f"{PENDING_SMOKE_IMAGE_REF_PREFIX}{time.time_ns()}"
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "CLI pending meal",
                "raw_summary": "CLI pending meal summary",
                "confidence": 0.9,
                "totals": {
                    "calories_kcal": 321,
                    "protein_g": 22,
                    "fat_g": 9,
                    "carbs_g": 31,
                },
                "items": [{"name": "CLI pending meal"}],
            },
            ensure_ascii=False,
        )
    )
    diary.stage_pending_meal(
        user_id=int(user_id),
        source=source,
        record=record,
        image_ref=image_ref,
        occurred_at=base_now,
        now=base_now,
        expires_at=(base_now - timedelta(minutes=1)) if expired else None,
    )
    return image_ref


def simulate_local_pending_reply(
    reply: str,
    *,
    user_id: int,
    db_path: str | Path | None = None,
    now: datetime | None = None,
) -> str:
    diary = HealBiteNutritionDiary(
        db_path=resolve_healbite_db_path(db_path),
        background_write=False,
    )
    pending = diary.get_pending_meal(user_id, now=now, include_expired=True)
    if pending is None:
        return "Не нашел ожидающую запись. Попробуй отправить фото ещё раз."
    normalized = normalize_simulation_text(reply).casefold()
    if diary.is_pending_meal_expired(pending, now=now):
        diary.clear_pending_meal(user_id)
        return format_pending_meal_expired_reply()
    if normalized in {"да", "ага", "ок", "окей", "yes", "y", "сохрани", "сохраняй"}:
        result = diary.confirm_pending_meal(user_id, now=now)
        if result.status == "missing":
            return "Не нашел ожидающую запись. Попробуй отправить фото ещё раз."
        if result.status == "expired":
            return format_pending_meal_expired_reply()
        summary = diary.get_daily_summary(user_id=user_id)
        return format_pending_meal_saved_reply(
            summary,
            duplicate=bool(result.duplicate),
        )
    if normalized in {"нет", "неа", "отмена", "отмени", "cancel", "no", "n"}:
        diary.clear_pending_meal(user_id)
        return format_pending_meal_cancelled_reply()
    return format_pending_meal_wait_reply()


def run_local_pending_smoke(
    *,
    db_path: str | Path | None = None,
    user_id: int = PENDING_SMOKE_USER_ID,
    source: str = PENDING_SMOKE_SOURCE,
    fail_after_step: str | None = None,
) -> list[str]:
    resolved = resolve_healbite_db_path(db_path)
    markers: list[str] = []
    pending_error: Exception | None = None
    base_now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)

    _cleanup_synthetic_pending_state(
        db_path=resolved,
        user_id=user_id,
        source=source,
    )
    try:
        _seed_synthetic_pending_row(
            db_path=resolved,
            user_id=user_id,
            source=source,
            now=base_now,
            expired=False,
        )
        cancel_report = simulate_local_pending_reply(
            "Нет",
            user_id=user_id,
            db_path=resolved,
            now=base_now,
        )
        if (
            "Отменено" not in cancel_report
            or _count_pending_rows(db_path=resolved, user_id=user_id) != 0
            or _count_synthetic_pending_nutrition_rows(
                db_path=resolved,
                user_id=user_id,
                source=source,
            ) != 0
        ):
            raise CLIError("Pending cancel smoke failed.")
        markers.append("pending_cancel_ok")
        _maybe_fail_correction_smoke(fail_after_step, "cancel")

        _seed_synthetic_pending_row(
            db_path=resolved,
            user_id=user_id,
            source=source,
            now=base_now,
            expired=False,
        )
        confirm_report = simulate_local_pending_reply(
            "Да",
            user_id=user_id,
            db_path=resolved,
            now=base_now + timedelta(minutes=5),
        )
        confirmed_summary = compute_nutrition_diary_summary(
            db_path=resolved,
            user_id=user_id,
            now=base_now + timedelta(minutes=5),
            days=1,
        )
        if (
            "Сохранено" not in confirm_report
            or _count_pending_rows(db_path=resolved, user_id=user_id) != 0
            or _count_synthetic_pending_nutrition_rows(
                db_path=resolved,
                user_id=user_id,
                source=source,
            ) != 1
            or float(confirmed_summary["entries"][-1]["calories_kcal"] or 0.0) != 321.0
        ):
            raise CLIError("Pending confirm smoke failed.")
        markers.append("pending_confirm_ok")
        _maybe_fail_correction_smoke(fail_after_step, "confirm")

        _cleanup_synthetic_pending_state(
            db_path=resolved,
            user_id=user_id,
            source=source,
        )
        _seed_synthetic_pending_row(
            db_path=resolved,
            user_id=user_id,
            source=source,
            now=base_now,
            expired=True,
        )
        expired_report = simulate_local_pending_reply(
            "Да",
            user_id=user_id,
            db_path=resolved,
            now=base_now + timedelta(hours=3),
        )
        if (
            "истекло" not in expired_report.casefold()
            or _count_pending_rows(db_path=resolved, user_id=user_id) != 0
            or _count_synthetic_pending_nutrition_rows(
                db_path=resolved,
                user_id=user_id,
                source=source,
            ) != 0
        ):
            raise CLIError("Pending TTL smoke failed.")
        markers.append("pending_ttl_ok")
    except Exception as exc:
        pending_error = exc
    finally:
        pending_rows, nutrition_rows = _cleanup_synthetic_pending_state(
            db_path=resolved,
            user_id=user_id,
            source=source,
        )
        if pending_rows != 0 or nutrition_rows != 0:
            raise CLIError("Synthetic pending smoke cleanup failed.") from pending_error
        markers.append("cleanup_ok")

    if pending_error is not None:
        raise pending_error
    return markers


def run_local_profile_smoke(
    *,
    db_path: str | Path | None = None,
    user_id: int = PROFILE_SMOKE_USER_ID,
) -> list[str]:
    resolved = resolve_healbite_db_path(db_path)
    store = HealBiteUserProfileStore(db_path=resolved)
    markers: list[str] = []

    store.delete_user_profile(int(user_id))
    try:
        if store.get_user_profile(int(user_id)) is not None:
            raise CLIError("Synthetic profile cleanup failed before smoke.")

        prompt = store.begin_onboarding(
            user_id=int(user_id),
            username="cli-profile",
        )
        if "норму калорий" not in prompt.casefold():
            raise CLIError("Profile onboarding start smoke failed.")
        markers.append("profile_onboarding_started_ok")

        reply = store.handle_onboarding_reply(
            user_id=int(user_id),
            text="2000 ккал",
            username="cli-profile",
        )
        if reply is None or reply.status != "completed":
            raise CLIError("Profile onboarding completion smoke failed.")
        markers.append("profile_saved_ok")

        profile = store.get_user_profile(int(user_id))
        report = format_healbite_profile_report(profile)
        if profile is None or profile.daily_kcal_target != 2000 or "2000 ккал" not in report:
            raise CLIError("Profile render smoke failed.")
        markers.append("profile_render_ok")
    finally:
        store.delete_user_profile(int(user_id))
        if store.get_user_profile(int(user_id)) is not None or store.get_onboarding_state(int(user_id)) is not None:
            raise CLIError("Synthetic profile smoke cleanup failed.")
        markers.append("cleanup_ok")

    return markers


def simulate_local_message(
    text: str,
    *,
    user_id: int | None = None,
    allow_write: bool = False,
    db_path: str | Path | None = None,
) -> str:
    normalized = normalize_simulation_text(text)
    lowered = normalized.lower()
    correction_intent = parse_diary_correction_intent(normalized)
    if correction_intent is not None:
        if user_id is None:
            return (
                "This simulation needs --user-id for a user-scoped diary correction.\n"
                "LLM and external calls are disabled by default for this diagnostic path."
            )
        if not allow_write:
            return _read_only_write_guard(correction_intent)
        return _apply_local_correction(
            intent=correction_intent,
            user_id=int(user_id),
            db_path=db_path,
        )
    if lowered in READ_ONLY_DIARY_PHRASES:
        if user_id is None:
            return (
                "\u0423\u043a\u0430\u0436\u0438 --user-id, \u0447\u0442\u043e\u0431\u044b \u044f \u043f\u043e\u043a\u0430\u0437\u0430\u043b "
                "\u043b\u043e\u043a\u0430\u043b\u044c\u043d\u0443\u044e \u0441\u0432\u043e\u0434\u043a\u0443 \u0434\u043d\u0435\u0432\u043d\u0438\u043a\u0430."
            )
        return _render_local_diary_report(
            user_id=int(user_id),
            days=1,
            db_path=db_path,
        )
    if lowered in AMBIGUOUS_DIARY_PHRASES:
        return _render_diary_clarification()
    if lowered not in SUPPORTED_SIMULATION_COMMANDS:
        supported = ", ".join(sorted(SUPPORTED_SIMULATION_COMMANDS))
        return (
            f"Unsupported for local simulation: {normalized or '<empty>'}\n"
            "LLM and external calls are disabled by default for this diagnostic path.\n"
            f"Supported local commands: {supported}"
        )
    if lowered in STATE_CHANGING_SIMULATION_COMMANDS and not allow_write:
        return (
            f"Simulated {normalized}\n"
            "This command changes state. Use --allow-write to execute."
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
    if lowered == "/profile":
        if user_id is None:
            return (
                "Simulated /profile\n"
                "No DB-backed user profile was rendered because --user-id was not provided."
            )
        profile = HealBiteUserProfileStore(db_path=db_path).get_user_profile(int(user_id))
        return (
            f"Simulated {normalized} for user_id={user_id}\n"
            f"{format_healbite_profile_report(profile)}"
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
        self.repo_root = repo_root or REPO_ROOT
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
            f"- undo_probe_ok: {result.get('undo_probe_ok', False)}",
            f"- encoding_ok: {result.get('encoding_ok', False)}",
        ]
        columns = result.get("columns") or []
        if columns:
            lines.append(f"- columns: {', '.join(columns)}")
        preview = result.get("summary_preview")
        if preview:
            lines.extend(["", "Probe summary preview:", preview])
        undo_preview = result.get("undo_preview")
        if undo_preview:
            lines.extend(["", "Undo preview:", undo_preview])
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

    def cmd_simulate_message(
        self,
        text: str,
        *,
        user_id: int | None = None,
        allow_write: bool = False,
    ) -> str:
        lowered = normalize_simulation_text(text).lower()
        if (
            parse_diary_correction_intent(text) is not None
            or lowered in READ_ONLY_DIARY_PHRASES
            or lowered in AMBIGUOUS_DIARY_PHRASES
        ):
            return simulate_local_message(text, user_id=user_id, allow_write=allow_write)
        if lowered in STATE_CHANGING_SIMULATION_COMMANDS:
            if not allow_write:
                return simulate_local_message(text, user_id=user_id, allow_write=False)
            if user_id is None:
                return (
                    f"Simulated {normalize_simulation_text(text)}\n"
                    "This command changes state. Pass --user-id together with --allow-write to execute."
                )
            result = self._docker_exec_python(
                _SIMULATE_UNDO_MEAL_CODE_TEMPLATE.format(user_id=int(user_id)),
                user="10000:10000",
            )
            if not result.get("ok", False):
                detail = result.get("detail", "Local undo simulation failed.")
                return f"Simulated {normalize_simulation_text(text)}\n{detail}"
            return (
                f"Simulated {normalize_simulation_text(text)} for user_id={user_id}\n"
                f"{result.get('text', '').strip()}"
            )
        if user_id is None or lowered not in {
            "/diary",
            "/diary 7d",
            "/stats",
            "/stats 7d",
        }:
            return simulate_local_message(text, user_id=user_id, allow_write=allow_write)
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

    def cmd_test_correction(self) -> str:
        return "\n".join(run_local_correction_smoke())

    def cmd_test_pending(self) -> str:
        return "\n".join(run_local_pending_smoke())

    def cmd_test_profile(self) -> str:
        return "\n".join(run_local_profile_smoke())


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
    HealBiteNutritionDiary,
    NUTRITION_LOG_TABLE,
    compute_nutrition_diary_summary,
    format_nutrition_diary_report,
    format_undo_meal_report,
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
    "undo_probe_ok": False,
    "encoding_ok": False,
    "columns": [],
    "summary_preview": "",
    "undo_preview": "",
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
        undo_result = HealBiteNutritionDiary(db_path=db_path, background_write=False).delete_last_meal(
            probe_user_id
        )
        result["undo_probe_ok"] = bool(undo_result.deleted and undo_result.sqlite_id == probe_id)
        result["undo_preview"] = format_undo_meal_report(undo_result)
        result["encoding_ok"] = result["encoding_ok"] and "????" not in result["undo_preview"]
        conn.execute(
            f"DELETE FROM {NUTRITION_LOG_TABLE} WHERE user_id = ? AND image_ref = ?",
            (probe_user_id, probe_ref),
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


_SIMULATE_UNDO_MEAL_CODE_TEMPLATE = r"""
from __future__ import annotations
import json

from gateway.healbite_nutrition_diary import HealBiteNutritionDiary, format_undo_meal_report, resolve_healbite_db_path

user_id = {user_id}
result = {{"ok": False, "text": "", "detail": ""}}
try:
    diary = HealBiteNutritionDiary(db_path=resolve_healbite_db_path(), background_write=False)
    undo_result = diary.delete_last_meal(user_id=user_id)
    result["ok"] = True
    result["text"] = format_undo_meal_report(undo_result)
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
    subparsers.add_parser(
        "test-correction",
        help="Run a deterministic synthetic smoke-test for diary correction UX",
    )
    subparsers.add_parser(
        "test-pending",
        help="Run a deterministic synthetic smoke-test for pending meal confirmation",
    )
    subparsers.add_parser(
        "test-profile",
        help="Run a deterministic synthetic smoke-test for HealBite onboarding/profile flow",
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
    simulate_parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Allow state-changing simulation commands to execute",
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
    if args.command == "test-correction":
        print(cli.cmd_test_correction())
        return 0
    if args.command == "test-pending":
        print(cli.cmd_test_pending())
        return 0
    if args.command == "test-profile":
        print(cli.cmd_test_profile())
        return 0
    if args.command == "check-admins":
        print(cli.cmd_check_admins())
        return 0
    if args.command == "inspect-profile":
        print(cli.cmd_inspect_profile(args.user_id))
        return 0
    if args.command == "simulate-message":
        print(
            cli.cmd_simulate_message(
                args.text,
                user_id=args.user_id,
                allow_write=args.allow_write,
            )
        )
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
