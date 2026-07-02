from __future__ import annotations

WEIGHT_REMINDER_SETTINGS_TABLE = "weight_reminder_settings"
WEIGHT_REMINDER_DELIVERIES_TABLE = "weight_reminder_deliveries"
WEIGHT_REMINDER_CANONICAL_TABLES = (
    WEIGHT_REMINDER_SETTINGS_TABLE,
    WEIGHT_REMINDER_DELIVERIES_TABLE,
)


class NonCanonicalReminderTableName(ValueError):
    pass


def assert_canonical_reminder_tables(table_names: tuple[str, ...] | list[str] | set[str]) -> None:
    configured = set(table_names)
    canonical = set(WEIGHT_REMINDER_CANONICAL_TABLES)
    if configured != canonical:
        raise NonCanonicalReminderTableName("NON-CANONICAL REMINDER TABLE NAME IN AUDIT TOOLING")
