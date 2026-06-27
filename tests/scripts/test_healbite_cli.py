from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway.healbite_nutrition_diary import (
    HealBiteNutritionDiary,
    compute_nutrition_diary_summary,
    normalize_nutrition_payload,
)


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "healbite_cli.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("healbite_cli", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
healbite_cli = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = healbite_cli
SCRIPT_SPEC.loader.exec_module(healbite_cli)


def _seed_record(
    db_path: Path,
    *,
    user_id: int,
    meal_name: str,
    calories_kcal: float,
    protein_g: float = 20,
    fat_g: float = 10,
    carbs_g: float = 30,
    image_ref: str | None = None,
):
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    record = normalize_nutrition_payload(
        (
            "{"
            f"\"is_food\": true, \"meal_name\": \"{meal_name}\", "
            f"\"raw_summary\": \"{meal_name} summary\", "
            "\"confidence\": 0.9, "
            f"\"totals\": {{\"calories_kcal\": {calories_kcal}, \"protein_g\": {protein_g}, "
            f"\"fat_g\": {fat_g}, \"carbs_g\": {carbs_g}}}, "
            f"\"items\": [{{\"name\": \"{meal_name}\"}}]"
            "}"
        )
    )
    diary.save_record(
        user_id=user_id,
        source="test",
        record=record,
        image_ref=image_ref or f"test:{user_id}:{meal_name}",
        occurred_at=None,
    )


def _count_rows(db_path: Path, *, user_id: int, source: str) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM nutrition_log WHERE user_id = ? AND source = ?",
            (user_id, source),
        ).fetchone()
    return int(row[0] if row else 0)


def _seed_pending(
    db_path: Path,
    *,
    user_id: int,
    meal_name: str = "pending meal",
    calories_kcal: float = 321,
    source: str = "cli_pending_smoke",
    expired: bool = False,
):
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    record = normalize_nutrition_payload(
        healbite_cli.json.dumps(
            {
                "is_food": True,
                "meal_name": meal_name,
                "raw_summary": f"{meal_name} summary",
                "confidence": 0.9,
                "totals": {
                    "calories_kcal": calories_kcal,
                    "protein_g": 20,
                    "fat_g": 10,
                    "carbs_g": 30,
                },
                "items": [{"name": meal_name}],
            },
            ensure_ascii=False,
        )
    )
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    diary.stage_pending_meal(
        user_id=user_id,
        source=source,
        record=record,
        image_ref=f"pending:{user_id}:{meal_name}",
        occurred_at=now,
        now=now,
        expires_at=(now - timedelta(minutes=1)) if expired else None,
    )


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


def test_build_parser_parses_simulate_message_allow_write():
    parser = healbite_cli.build_parser()
    args = parser.parse_args([
        "simulate-message",
        "/undo_meal",
        "--user-id",
        "248875361",
        "--allow-write",
    ])
    assert args.command == "simulate-message"
    assert args.text == "/undo_meal"
    assert args.user_id == 248875361
    assert args.allow_write is True


def test_build_parser_parses_test_correction():
    parser = healbite_cli.build_parser()
    args = parser.parse_args(["test-correction"])
    assert args.command == "test-correction"


def test_build_parser_parses_test_pending():
    parser = healbite_cli.build_parser()
    args = parser.parse_args(["test-pending"])
    assert args.command == "test-pending"


def test_build_parser_parses_test_water():
    parser = healbite_cli.build_parser()
    args = parser.parse_args(["test-water"])
    assert args.command == "test-water"


def test_run_local_water_smoke_uses_temp_db(tmp_path):
    markers = healbite_cli.run_local_water_smoke(db_path=tmp_path / "water.db")
    assert "water_parser_ok" in markers
    assert "water_summary_ok" in markers
    assert "water_undo_ok" in markers


def test_build_parser_parses_free_text_simulation_with_allow_write():
    parser = healbite_cli.build_parser()
    args = parser.parse_args(
        [
            "simulate-message",
            "исправь последнюю запись на 400 ккал",
            "--user-id",
            "248875361",
            "--allow-write",
        ]
    )
    assert args.command == "simulate-message"
    assert args.user_id == 248875361
    assert args.allow_write is True


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


def test_simulate_message_blocks_state_change_without_allow_write():
    report = healbite_cli.simulate_local_message("/undo_meal")
    assert "This command changes state. Use --allow-write to execute." in report


def test_cmd_simulate_message_state_change_without_allow_write_stays_local(monkeypatch):
    cli = healbite_cli.HealBiteCLI(repo_root=Path("."), runner=None)
    monkeypatch.setattr(
        cli,
        "_docker_exec_python",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("docker should not run")),
    )
    report = cli.cmd_simulate_message("/undo_meal")
    assert "This command changes state. Use --allow-write to execute." in report


def test_cmd_simulate_message_state_change_requires_user_id_for_write(monkeypatch):
    cli = healbite_cli.HealBiteCLI(repo_root=Path("."), runner=None)
    monkeypatch.setattr(
        cli,
        "_docker_exec_python",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("docker should not run")),
    )
    report = cli.cmd_simulate_message("/undo_meal", allow_write=True)
    assert "Pass --user-id together with --allow-write to execute." in report


def test_cmd_simulate_message_free_text_correction_stays_local(monkeypatch):
    cli = healbite_cli.HealBiteCLI(repo_root=Path("."), runner=None)
    monkeypatch.setattr(
        cli,
        "_docker_exec_python",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("docker should not run")
        ),
    )
    monkeypatch.setattr(
        healbite_cli,
        "simulate_local_message",
        lambda *args, **kwargs: "LOCAL",
    )
    report = cli.cmd_simulate_message(
        "исправь последнюю запись на 400 ккал",
        user_id=248875361,
        allow_write=True,
    )
    assert report == "LOCAL"


def test_cmd_test_correction_returns_marker_lines(monkeypatch):
    cli = healbite_cli.HealBiteCLI(repo_root=Path("."), runner=None)
    monkeypatch.setattr(
        healbite_cli,
        "run_local_correction_smoke",
        lambda **kwargs: [
            "correction_guard_ok",
            "set_calories_ok",
            "add_calories_ok",
            "rename_ok",
            "read_only_ok",
            "ambiguous_noop_ok",
            "cleanup_ok",
        ],
    )
    report = cli.cmd_test_correction()
    assert "correction_guard_ok" in report
    assert "cleanup_ok" in report


def test_cmd_test_pending_returns_marker_lines(monkeypatch):
    cli = healbite_cli.HealBiteCLI(repo_root=Path("."), runner=None)
    monkeypatch.setattr(
        healbite_cli,
        "run_local_pending_smoke",
        lambda **kwargs: [
            "pending_cancel_ok",
            "pending_confirm_ok",
            "pending_ttl_ok",
            "cleanup_ok",
        ],
    )
    report = cli.cmd_test_pending()
    assert "pending_confirm_ok" in report
    assert "cleanup_ok" in report


def test_cmd_test_profile_returns_marker_lines(monkeypatch):
    cli = healbite_cli.HealBiteCLI(repo_root=Path("."), runner=None)
    monkeypatch.setattr(
        healbite_cli,
        "run_local_profile_smoke",
        lambda **kwargs: [
            "profile_onboarding_started_ok",
            "profile_saved_ok",
            "profile_render_ok",
            "cleanup_ok",
        ],
    )
    report = cli.cmd_test_profile()
    assert "profile_saved_ok" in report
    assert "cleanup_ok" in report


def test_simulate_message_correction_requires_allow_write(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_record(db_path, user_id=11, meal_name="omelet", calories_kcal=321)

    report = healbite_cli.simulate_local_message(
        "исправь последнюю запись на 400 ккал",
        user_id=11,
        db_path=db_path,
    )

    summary = compute_nutrition_diary_summary(db_path=db_path, user_id=11)
    assert "This command changes state. Use --allow-write to execute." in report
    assert summary["entries"][-1]["calories_kcal"] == 321


def test_simulate_message_correction_set_calories_updates_only_latest_record(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_record(
        db_path,
        user_id=21,
        meal_name="first",
        calories_kcal=250,
        image_ref="test:21:first",
    )
    _seed_record(
        db_path,
        user_id=21,
        meal_name="latest",
        calories_kcal=321,
        image_ref="test:21:latest",
    )

    report = healbite_cli.simulate_local_message(
        "исправь последнюю запись на 400 ккал",
        user_id=21,
        allow_write=True,
        db_path=db_path,
    )

    summary = compute_nutrition_diary_summary(db_path=db_path, user_id=21)
    assert report.startswith("✅ Исправил последнюю запись:")
    assert "latest — 400 ккал" in report
    assert "user_facing_reply" not in report
    assert summary["entries"][0]["calories_kcal"] == 250
    assert summary["entries"][-1]["calories_kcal"] == 400


def test_simulate_message_correction_add_calories_updates_latest_record(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_record(db_path, user_id=31, meal_name="latest", calories_kcal=400)

    report = healbite_cli.simulate_local_message(
        "добавь к последней записи 100 ккал",
        user_id=31,
        allow_write=True,
        db_path=db_path,
    )

    summary = compute_nutrition_diary_summary(db_path=db_path, user_id=31)
    assert "latest — 500 ккал" in report
    assert summary["entries"][-1]["calories_kcal"] == 500


def test_simulate_message_correction_rename_updates_latest_record(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_record(db_path, user_id=41, meal_name="latest", calories_kcal=400)

    report = healbite_cli.simulate_local_message(
        "переименуй последнюю запись в борщ",
        user_id=41,
        allow_write=True,
        db_path=db_path,
    )

    summary = compute_nutrition_diary_summary(db_path=db_path, user_id=41)
    assert "борщ — 400 ккал" in report
    assert summary["entries"][-1]["meal_name"] == "борщ"


def test_simulate_message_read_only_phrase_returns_summary_without_mutation(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_record(db_path, user_id=51, meal_name="salad", calories_kcal=280)

    report = healbite_cli.simulate_local_message(
        "что у меня сегодня в дневнике?",
        user_id=51,
        db_path=db_path,
    )

    summary = compute_nutrition_diary_summary(db_path=db_path, user_id=51)
    assert "Твой дневник за сегодня" in report
    assert "salad" in report
    assert summary["entries"][-1]["calories_kcal"] == 280


def test_simulate_message_ambiguous_phrase_does_not_mutate(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_record(db_path, user_id=61, meal_name="meal", calories_kcal=280)

    report = healbite_cli.simulate_local_message(
        "исправь ошибку",
        user_id=61,
        db_path=db_path,
    )

    summary = compute_nutrition_diary_summary(db_path=db_path, user_id=61)
    assert "Нужно конкретное исправление" in report
    assert summary["entries"][-1]["calories_kcal"] == 280


def test_simulate_message_user_id_isolation(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_record(db_path, user_id=71, meal_name="user-one", calories_kcal=280)
    _seed_record(db_path, user_id=72, meal_name="user-two", calories_kcal=310)

    healbite_cli.simulate_local_message(
        "исправь последнюю запись на 400 ккал",
        user_id=71,
        allow_write=True,
        db_path=db_path,
    )

    summary_one = compute_nutrition_diary_summary(db_path=db_path, user_id=71)
    summary_two = compute_nutrition_diary_summary(db_path=db_path, user_id=72)
    assert summary_one["entries"][-1]["calories_kcal"] == 400
    assert summary_two["entries"][-1]["calories_kcal"] == 310


def test_run_local_correction_smoke_outputs_all_markers_and_cleans_up(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_record(
        db_path,
        user_id=424242,
        meal_name="real-user-meal",
        calories_kcal=280,
        image_ref="test:424242:real-user",
    )

    markers = healbite_cli.run_local_correction_smoke(db_path=db_path)

    assert markers == [
        "correction_guard_ok",
        "set_calories_ok",
        "add_calories_ok",
        "rename_ok",
        "read_only_ok",
        "ambiguous_noop_ok",
        "cleanup_ok",
    ]
    assert _count_rows(
        db_path,
        user_id=healbite_cli.CORRECTION_SMOKE_USER_ID,
        source=healbite_cli.CORRECTION_SMOKE_SOURCE,
    ) == 0
    real_user_summary = compute_nutrition_diary_summary(db_path=db_path, user_id=424242)
    assert real_user_summary["entries"][-1]["meal_name"] == "real-user-meal"
    assert real_user_summary["entries"][-1]["calories_kcal"] == 280


def test_run_local_correction_smoke_cleans_up_on_failure(tmp_path):
    db_path = tmp_path / "healbite.db"

    try:
        healbite_cli.run_local_correction_smoke(
            db_path=db_path,
            fail_after_step="set",
        )
    except RuntimeError as exc:
        assert "Injected failure after step: set" in str(exc)
    else:
        raise AssertionError("Expected injected correction smoke failure")

    assert _count_rows(
        db_path,
        user_id=healbite_cli.CORRECTION_SMOKE_USER_ID,
        source=healbite_cli.CORRECTION_SMOKE_SOURCE,
    ) == 0


def test_simulate_local_pending_reply_cancel_clears_state(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_pending(db_path, user_id=81)

    report = healbite_cli.simulate_local_pending_reply(
        "Нет",
        user_id=81,
        db_path=db_path,
        now=datetime(2026, 6, 18, 12, 5, tzinfo=timezone.utc),
    )

    assert "Отменено" in report
    assert healbite_cli._count_pending_rows(db_path=db_path, user_id=81) == 0
    assert _count_rows(db_path, user_id=81, source="cli_pending_smoke") == 0


def test_simulate_local_pending_reply_confirm_writes_to_nutrition_log(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_pending(db_path, user_id=82)

    report = healbite_cli.simulate_local_pending_reply(
        "Да",
        user_id=82,
        db_path=db_path,
        now=datetime(2026, 6, 18, 12, 5, tzinfo=timezone.utc),
    )

    summary = compute_nutrition_diary_summary(
        db_path=db_path,
        user_id=82,
        now=datetime(2026, 6, 18, 12, 5, tzinfo=timezone.utc),
    )
    assert "Сохранено" in report
    assert healbite_cli._count_pending_rows(db_path=db_path, user_id=82) == 0
    assert _count_rows(db_path, user_id=82, source="cli_pending_smoke") == 1
    assert summary["entries"][-1]["calories_kcal"] == 321


def test_simulate_local_pending_reply_expired_rejects_without_write(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_pending(db_path, user_id=83, expired=True)

    report = healbite_cli.simulate_local_pending_reply(
        "Да",
        user_id=83,
        db_path=db_path,
        now=datetime(2026, 6, 18, 15, 0, tzinfo=timezone.utc),
    )

    assert "истекло" in report.casefold()
    assert healbite_cli._count_pending_rows(db_path=db_path, user_id=83) == 0
    assert _count_rows(db_path, user_id=83, source="cli_pending_smoke") == 0


def test_run_local_pending_smoke_outputs_all_markers_and_cleans_up(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_record(
        db_path,
        user_id=515151,
        meal_name="real-user-meal",
        calories_kcal=280,
        image_ref="test:515151:real-user",
    )

    markers = healbite_cli.run_local_pending_smoke(db_path=db_path)

    assert markers == [
        "pending_cancel_ok",
        "pending_confirm_ok",
        "pending_ttl_ok",
        "cleanup_ok",
    ]
    assert healbite_cli._count_pending_rows(
        db_path=db_path,
        user_id=healbite_cli.PENDING_SMOKE_USER_ID,
    ) == 0
    assert _count_rows(
        db_path,
        user_id=healbite_cli.PENDING_SMOKE_USER_ID,
        source=healbite_cli.PENDING_SMOKE_SOURCE,
    ) == 0
    real_user_summary = compute_nutrition_diary_summary(db_path=db_path, user_id=515151)
    assert real_user_summary["entries"][-1]["meal_name"] == "real-user-meal"
    assert real_user_summary["entries"][-1]["calories_kcal"] == 280


def test_run_local_pending_smoke_cleans_up_on_failure(tmp_path):
    db_path = tmp_path / "healbite.db"

    try:
        healbite_cli.run_local_pending_smoke(
            db_path=db_path,
            fail_after_step="confirm",
        )
    except RuntimeError as exc:
        assert "Injected failure after step: confirm" in str(exc)
    else:
        raise AssertionError("Expected injected pending smoke failure")

    assert healbite_cli._count_pending_rows(
        db_path=db_path,
        user_id=healbite_cli.PENDING_SMOKE_USER_ID,
    ) == 0
    assert _count_rows(
        db_path,
        user_id=healbite_cli.PENDING_SMOKE_USER_ID,
        source=healbite_cli.PENDING_SMOKE_SOURCE,
    ) == 0


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


def test_simulate_profile_message_renders_local_profile(tmp_path):
    db_path = tmp_path / "healbite.db"
    store = healbite_cli.HealBiteUserProfileStore(db_path=db_path)
    store.upsert_user_profile(user_id=77, username="oleg", daily_kcal_target=2000)

    report = healbite_cli.simulate_local_message(
        "/profile",
        user_id=77,
        db_path=db_path,
    )

    assert "Ваш профиль" in report
    assert "2000 ккал" in report
    assert "справочный характер" in report
