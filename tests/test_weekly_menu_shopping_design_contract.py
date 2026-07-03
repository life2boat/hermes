from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ADR = REPO_ROOT / "docs" / "adr" / "ADR-0073-household-weekly-menu-shopping-domain.md"
PLAN = REPO_ROOT / "docs" / "design" / "healbite-weekly-menu-shopping-implementation-plan.md"
TELEGRAM = REPO_ROOT / "gateway" / "platforms" / "telegram.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _require(text: str, needles: list[str], *, label: str) -> None:
    for needle in needles:
        assert needle in text, f"missing {label}: {needle}"


def test_adr_defines_core_household_menu_and_shopping_contract() -> None:
    text = _read(ADR)
    _require(
        text,
        [
            "# ADR-0073: Household Weekly Menu and Shopping Domain",
            "## Aggregate Boundaries",
            "## Identity and Ownership",
            "## Authorization",
            "## Week Semantics",
            "## Weekly-Menu Lifecycle",
            "## Meal-Entry Model",
            "## Member Nutrition Bridge",
            "## Shopping-List Lifecycle",
            "## Manual Shopping Items",
            "## Versioning and Concurrency",
            "## Idempotency",
            "## Deletion and Archival",
            "## Feature Flags",
            "## Telegram Boundary",
            "## Privacy",
            "## Migration Strategy",
            "## Production Rollout",
            "## Threat Model",
            "## Explicit Non-Goals",
        ],
        label="ADR section",
    )


def test_adr_locks_household_ownership_and_member_authorization() -> None:
    text = _read(ADR)
    _require(
        text,
        [
            "weekly menu belongs to `household`",
            "shopping list belongs to `household`",
            "actor access is resolved through an active linked `household member`",
            "linked_user_id is not a household ID",
            "member_id is not interchangeable with user_id",
            "HouseholdAuthorizationContext",
            "household_id",
            "actor member context",
            "expected authorization scope",
        ],
        label="ownership and authorization contract",
    )


def test_adr_locks_week_semantics_and_menu_key() -> None:
    text = _read(ADR)
    _require(
        text,
        [
            "week_start is a local calendar date",
            "week_start always represents Monday",
            "ISO date YYYY-MM-DD",
            "single normalization helper",
            "household_id + week_start",
            "one canonical menu lineage exists per `household_id + week_start`",
        ],
        label="week and menu contract",
    )


def test_adr_separates_menu_from_food_diary_and_preserves_manual_items() -> None:
    text = _read(ADR)
    _require(
        text,
        [
            "menu entry creation does not write to `nutrition_log`",
            "publishing a menu does not write to `nutrition_log`",
            "Manual items must not disappear on shopping regeneration",
            "preserve checked state",
            "never auto-merge incompatible units",
            "decimal string for persisted quantity values",
        ],
        label="diary and shopping preservation contract",
    )


def test_adr_requires_concurrency_idempotency_and_no_startup_writes() -> None:
    text = _read(ADR)
    _require(
        text,
        [
            "expected_version",
            "no hidden last-write-wins fallback",
            "idempotency token",
            "Duplicate Telegram callback deliveries",
            "must not create duplicate menus, lists, or items",
        ],
        label="concurrency and idempotency contract",
    )
    plan_text = _read(PLAN)
    _require(
        plan_text,
        [
            "No startup path may create menu or shopping aggregates.",
            "exact-image deployment with feature disabled",
        ],
        label="startup-write prohibition",
    )


def test_plan_defines_staged_rollout_and_default_disabled_flags() -> None:
    text = _read(PLAN)
    _require(
        text,
        [
            "### C1 - Weekly-Menu Schema and Store Core",
            "### C2 - Shopping Schema and Store Core",
            "### C3 - Feature-Disabled Runtime Services",
            "### C4 - Telegram Read-Only Menu UI Behind Allowlist",
            "### C5 - Controlled Menu Mutations and Generation",
            "### C6 - Shopping UI and Mutations",
            "### C7 - Family UI",
            "HEALBITE_WEEKLY_MENU_ENABLED=false",
            "HEALBITE_SHOPPING_LIST_ENABLED=false",
            "HEALBITE_FAMILY_UI_ENABLED=false",
            "additive schema initialization",
            "allowlist canary",
        ],
        label="implementation plan contract",
    )


def test_telegram_placeholders_remain_in_development_until_ui_sprints() -> None:
    text = _read(TELEGRAM)
    _require(
        text,
        [
            '"📋 Меню на неделю": "__placeholder__:weekly_menu"',
            '"🛒 Список покупок": "__placeholder__:shopping_list"',
            '"👨‍👩‍👧 Семья": "__placeholder__:family"',
            'HEALBITE_PLACEHOLDER_REPLY = "В разработке"',
        ],
        label="telegram placeholder contract",
    )
