"""Canonical SQLite database schema contract and semantic comparison evaluator."""

import hashlib
import sqlite3
from typing import Any, Dict, List, Tuple

EXPECTED_USER_VERSION: int = 0
EXPECTED_SCHEMA_DIGEST: str = (
    "99d49c97f776cbe4140f2fa352f438b4af6a83ba7fe4968f6299d17471f1fe5e"
)

EXPECTED_TABLES: Tuple[str, ...] = (
    "body_measurements",
    "families",
    "family_members",
    "food_logs",
    "healbite_inventory_items",
    "healbite_inventory_snapshots",
    "household_invitations",
    "household_members",
    "household_shopping_idempotency",
    "household_shopping_item_contributions",
    "household_shopping_items",
    "household_shopping_lists",
    "household_weekly_menu_entries",
    "household_weekly_menu_entry_ingredients",
    "household_weekly_menu_idempotency",
    "household_weekly_menu_series",
    "household_weekly_menus",
    "households",
    "meal_logs",
    "memory_analytics_logs",
    "memory_identity_metadata",
    "memory_os_conflicts",
    "memory_os_facts",
    "memory_os_facts_fts",
    "memory_os_facts_fts_config",
    "memory_os_facts_fts_data",
    "memory_os_facts_fts_docsize",
    "memory_os_facts_fts_idx",
    "memory_os_session_history",
    "memory_os_session_history_fts",
    "memory_os_session_history_fts_config",
    "memory_os_session_history_fts_data",
    "memory_os_session_history_fts_docsize",
    "memory_os_session_history_fts_idx",
    "memory_os_vector_sync_meta",
    "memory_os_vector_sync_outbox",
    "nutrition_log",
    "pending_meals",
    "planned_ingredients",
    "planned_meals",
    "profiles",
    "structured_user_facts",
    "usda_food_db",
    "user_inventory",
    "user_onboarding_state",
    "users",
    "water_intake_events",
    "water_logs",
    "water_pending_inputs",
    "weekly_menu_plans",
    "weight_entries",
    "weight_logs",
    "weight_pending_inputs",
    "weight_reminder_deliveries",
    "weight_reminder_settings",
)

EXPECTED_INDEXES: Tuple[str, ...] = (
    "idx_body_measurements_user_type_timestamp",
    "idx_family_members_telegram_id",
    "idx_food_logs_user_timestamp",
    "idx_healbite_inventory_items_snapshot",
    "idx_healbite_inventory_snapshots_household",
    "idx_healbite_inventory_snapshots_user",
    "idx_household_invitations_create_idempotency",
    "idx_household_invitations_household",
    "idx_household_invitations_incoming",
    "idx_household_invitations_pending_unique",
    "idx_household_members_active_linked_user",
    "idx_household_members_active_owner",
    "idx_household_members_id_household",
    "idx_household_shopping_contributions_item_source_unique",
    "idx_household_shopping_idempotency_unique",
    "idx_household_shopping_items_generated_dedup_unique",
    "idx_household_shopping_items_list_origin",
    "idx_household_shopping_items_list_position_unique",
    "idx_household_shopping_lists_active_per_household_week",
    "idx_household_shopping_lists_household_week_status",
    "idx_household_shopping_lists_id_household",
    "idx_meal_logs_user_timestamp",
    "idx_memory_analytics_logs_event_type_created_at",
    "idx_memory_analytics_logs_user_id_created_at",
    "idx_memory_os_facts_fact_uuid",
    "idx_memory_os_facts_lookup",
    "idx_memory_os_facts_rank",
    "idx_memory_os_facts_user_entity_key",
    "idx_memory_os_facts_user_id",
    "idx_memory_os_session_history_session_created",
    "idx_memory_os_session_history_user_created",
    "idx_memory_os_vector_sync_outbox_fact_revision",
    "idx_memory_os_vector_sync_outbox_ready",
    "idx_memory_os_vector_sync_outbox_uuid_revision",
    "idx_nutrition_log_user_image_ref",
    "idx_nutrition_log_user_occurred_at",
    "idx_pending_meals_expires_at",
    "idx_planned_ingredients_meal",
    "idx_planned_ingredients_missing",
    "idx_planned_meals_plan_day",
    "idx_profiles_onboarding_done",
    "idx_structured_user_facts_user_updated",
    "idx_user_inventory_user_name",
    "idx_water_intake_user_consumed_at",
    "idx_water_intake_user_idempotency",
    "idx_water_pending_expires_at",
    "idx_weekly_menu_entries_menu_local_date_slot_position",
    "idx_weekly_menu_entries_menu_slot_position_unique",
    "idx_weekly_menu_idempotency_unique",
    "idx_weekly_menu_ingredients_entry_position_unique",
    "idx_weekly_menu_plans_user_week",
    "idx_weekly_menu_revisions_id_household",
    "idx_weekly_menu_revisions_series_revision_unique",
    "idx_weekly_menu_revisions_single_approved",
    "idx_weekly_menu_revisions_single_draft",
    "idx_weekly_menu_revisions_single_published",
    "idx_weekly_menu_series_household_week_unique",
    "idx_weekly_menu_series_id_household",
    "idx_weight_entries_user_local_date",
    "idx_weight_entries_user_recorded_at",
    "idx_weight_pending_expires_at",
    "idx_weight_reminder_deliveries_retry",
    "idx_weight_reminder_deliveries_status_claim",
    "idx_weight_reminder_deliveries_user_scheduled",
    "idx_weight_reminder_settings_due",
    "idx_weight_reminder_settings_user_state",
)

EXPECTED_TRIGGERS: Tuple[str, ...] = (
    "memory_identity_metadata_immutable",
    "memory_identity_metadata_no_delete",
    "memory_os_facts_fts_ad",
    "memory_os_facts_fts_ai",
    "memory_os_facts_fts_au",
    "memory_os_facts_uuid_immutable",
    "memory_os_facts_uuid_insert",
    "memory_os_facts_uuid_update",
    "memory_os_session_history_ad",
    "memory_os_session_history_ai",
    "memory_os_session_history_au",
    "memory_os_vector_sync_outbox_uuid_immutable",
    "memory_os_vector_sync_outbox_uuid_insert",
    "memory_os_vector_sync_outbox_uuid_update",
)


def evaluate_database_schema(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Perform real semantic comparison of database against canonical schema contract."""
    # Integrity check
    try:
        integrity_cursor = conn.execute("PRAGMA integrity_check")
        integrity_row = integrity_cursor.fetchone()
        integrity_ok = (
            integrity_row is not None and str(integrity_row[0]).lower() == "ok"
        )
        integrity_status = "ok" if integrity_ok else "FAIL"
    except Exception:
        integrity_ok = False
        integrity_status = "FAIL"

    # Foreign key check
    try:
        fk_cursor = conn.execute("PRAGMA foreign_key_check")
        fk_violations = len(fk_cursor.fetchall())
    except Exception:
        fk_violations = 1

    # User version
    try:
        uv_cursor = conn.execute("PRAGMA user_version")
        actual_uv = int(uv_cursor.fetchone()[0])
    except Exception:
        actual_uv = -1

    # Objects from master/schema
    try:
        cursor = conn.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name"
        )
        schema_sql = "\n".join(row[0] for row in cursor.fetchall())
        actual_digest = hashlib.sha256(schema_sql.encode("utf-8")).hexdigest()
    except Exception:
        schema_sql = "ERROR"
        actual_digest = "error_digest"

    actual_tables = sorted(
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    actual_indexes = sorted(
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    )
    actual_triggers = sorted(
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name NOT LIKE 'sqlite_%'"
        )
    )

    # Semantic delta comparison
    if set(actual_tables) != set(EXPECTED_TABLES):
        schema_delta = "TABLE_DELTA"
    elif set(actual_indexes) != set(EXPECTED_INDEXES):
        schema_delta = "INDEX_DELTA"
    elif set(actual_triggers) != set(EXPECTED_TRIGGERS):
        schema_delta = "TRIGGER_DELTA"
    elif actual_digest != EXPECTED_SCHEMA_DIGEST:
        schema_delta = "UNKNOWN"
    else:
        schema_delta = "NONE"

    is_pass = (
        integrity_ok
        and fk_violations == 0
        and actual_uv == EXPECTED_USER_VERSION
        and schema_delta == "NONE"
        and actual_digest == EXPECTED_SCHEMA_DIGEST
    )

    migration_required = schema_delta != "NONE"

    return {
        "status": "PASS" if is_pass else "FAIL",
        "observed_schema": schema_sql,
        "digest": actual_digest,
        "user_version": actual_uv,
        "actual_user_version": actual_uv,
        "expected_user_version": EXPECTED_USER_VERSION,
        "actual_schema_digest": actual_digest,
        "expected_schema_digest": EXPECTED_SCHEMA_DIGEST,
        "schema_delta": schema_delta,
        "migration_required": migration_required,
        "integrity_status": integrity_status,
        "foreign_key_violation_count": fk_violations,
    }
