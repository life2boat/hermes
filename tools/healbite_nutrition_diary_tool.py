#!/usr/bin/env python3
from __future__ import annotations

import math

from gateway.healbite_nutrition_diary import get_default_nutrition_diary, resolve_healbite_db_path
from gateway.session_context import get_session_env
from tools.registry import registry, tool_error, tool_result


def check_update_last_meal_requirements() -> bool:
    return resolve_healbite_db_path().exists()


def _format_metric(value: float | int | None, unit: str) -> str | None:
    if value is None:
        return None
    numeric = float(value)
    if math.isfinite(numeric) and numeric.is_integer():
        rendered = str(int(numeric))
    else:
        rendered = f"{numeric:.1f}".rstrip("0").rstrip(".")
    return f"{rendered} {unit}"


def _build_update_last_meal_user_reply(*, meal_name: str | None, calories_kcal: float | int | None, protein_g: float | int | None, fat_g: float | int | None, carbs_g: float | int | None) -> str:
    title = (meal_name or "Последняя запись").strip().replace("\r", " ").replace("\n", " ")
    calories = _format_metric(calories_kcal, "ккал") or "без калорий"
    macro_values = {
        "Б": _format_metric(protein_g, "г"),
        "Ж": _format_metric(fat_g, "г"),
        "У": _format_metric(carbs_g, "г"),
    }
    macro_parts = [f"{label} {value}" for label, value in macro_values.items() if value is not None]
    macros_line = ""
    if macro_parts:
        macros_line = "\n" + " · ".join(macro_parts)

    return (
        "✅ Исправил последнюю запись:\n"
        f"{title} — {calories}"
        f"{macros_line}\n\n"
        "Вызови /diary, чтобы посмотреть итог за день."
    )


def update_last_meal_tool(
    new_meal_name: str | None = None,
    new_calories: int | None = None,
    new_protein: float | None = None,
    new_fat: float | None = None,
    new_carbs: float | None = None,
) -> str:
    user_id = get_session_env("HERMES_SESSION_USER_ID", "").strip()
    if not user_id:
        return tool_error(
            "update_last_meal is only available inside a user-scoped HealBite gateway session.",
            success=False,
        )

    try:
        result = get_default_nutrition_diary().update_last_meal(
            user_id=user_id,
            new_meal_name=new_meal_name,
            new_calories=new_calories,
            new_protein=new_protein,
            new_fat=new_fat,
            new_carbs=new_carbs,
        )
    except ValueError as exc:
        return tool_error(str(exc), success=False)

    if not result.updated:
        return tool_error(
            "\u0421\u0435\u0433\u043e\u0434\u043d\u044f \u0432 \u0434\u043d\u0435\u0432\u043d\u0438\u043a\u0435 \u043f\u043e\u043a\u0430 \u043d\u0435\u0442 \u0437\u0430\u043f\u0438\u0441\u0435\u0439 \u0434\u043b\u044f \u0438\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u044f.",
            success=False,
            code="diary_empty",
        )

    user_facing_reply = _build_update_last_meal_user_reply(
        meal_name=result.meal_name,
        calories_kcal=result.calories_kcal,
        protein_g=result.protein_g,
        fat_g=result.fat_g,
        carbs_g=result.carbs_g,
    )

    return tool_result(
        success=True,
        sqlite_id=result.sqlite_id,
        meal_name=result.meal_name,
        calories_kcal=result.calories_kcal,
        protein_g=result.protein_g,
        fat_g=result.fat_g,
        carbs_g=result.carbs_g,
        occurred_at=result.occurred_at,
        assistant_directive=(
            "Success. ОБЯЗАТЕЛЬНО ответь пользователю строго текстом из поля "
            "user_facing_reply. Не добавляй ничего от себя, не пересказывай JSON "
            "и не меняй формулировку."
        ),
        user_facing_reply=user_facing_reply,
    )


UPDATE_LAST_MEAL_SCHEMA = {
    "name": "update_last_meal",
    "description": (
        "Use this tool when the user asks to change, correct, rename, or adjust "
        "the KBJU/macros of their most recent meal entry for today. "
        "It can safely update only the latest diary entry for the current user. "
        "The current values can be checked by reviewing the diary or stats first. "
        "When the tool succeeds, reply to the user by copying the user_facing_reply "
        "field exactly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "new_meal_name": {
                "type": "string",
                "description": "Optional new meal name for the latest diary entry.",
            },
            "new_calories": {
                "type": "integer",
                "description": "Optional corrected calories in kcal for the latest diary entry.",
            },
            "new_protein": {
                "type": "number",
                "description": "Optional corrected protein value in grams.",
            },
            "new_fat": {
                "type": "number",
                "description": "Optional corrected fat value in grams.",
            },
            "new_carbs": {
                "type": "number",
                "description": "Optional corrected carbs value in grams.",
            },
        },
        "required": [],
    },
}


registry.register(
    name="update_last_meal",
    toolset="nutrition_diary",
    schema=UPDATE_LAST_MEAL_SCHEMA,
    handler=lambda args, **kw: update_last_meal_tool(
        new_meal_name=args.get("new_meal_name"),
        new_calories=args.get("new_calories"),
        new_protein=args.get("new_protein"),
        new_fat=args.get("new_fat"),
        new_carbs=args.get("new_carbs"),
    ),
    check_fn=check_update_last_meal_requirements,
    emoji="\U0001F37D",
)
