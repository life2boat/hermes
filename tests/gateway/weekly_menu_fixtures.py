"""Explicit fixtures for new valid weeks and read compatibility with legacy data."""

from dataclasses import replace

from gateway.healbite_weekly_menu_schema import week_dates
from gateway.healbite_weekly_menus import (
    WeeklyMenuEntryInput,
    WeeklyMenuIngredientInput,
)


def complete_weekly_entries(entries, *, week_start="2026-07-06"):
    """Complete a NEW publication fixture; never used by production code."""
    by_slot = {
        (
            entry.local_date,
            str(getattr(entry.meal_slot, "value", entry.meal_slot)),
        ): entry
        for entry in entries
    }
    result = []
    for day in week_dates(week_start):
        for slot in ("breakfast", "lunch", "dinner"):
            entry = by_slot.get(
                (day, slot),
                WeeklyMenuEntryInput(
                    day,
                    slot,
                    1,
                    entries[0].title if entries else "Synthetic meal",
                    origin=entries[0].origin if entries else "manual",
                ),
            )
            result.append(
                replace(
                    entry,
                    position=1,
                    servings=entry.servings or "1",
                    ingredients=entry.ingredients
                    or (
                        WeeklyMenuIngredientInput(
                            "Synthetic ingredient", "1", "g", "1"
                        ),
                    ),
                )
            )
    return result


def seed_legacy_published_revision(
    store,
    context,
    revision_id,
    *,
    expected_series_version,
    expected_revision_version,
    idempotency_key,
):
    """Model pre-contract published history for READ/SHOPPING compatibility tests.

    Deliberately NOT a call to the now-stricter publication API. Tests of new
    publication must use the real API and complete_weekly_entries instead.
    No validators are patched. Only the test's synthetic SQLite DB is touched.
    """
    before = store.get_weekly_menu_revision(context, revision_id)
    assert before.series.version == expected_series_version
    assert before.revision.version == expected_revision_version
    with store._connect() as conn:
        conn.execute(
            "UPDATE household_weekly_menus SET status='archived', archived_at=CURRENT_TIMESTAMP "
            "WHERE series_id=? AND status='published'",
            (before.series.id,),
        )
        conn.execute(
            "UPDATE household_weekly_menus SET status='published', published_at=CURRENT_TIMESTAMP, "
            "version=version+1 WHERE id=?",
            (revision_id,),
        )
        conn.execute(
            "UPDATE household_weekly_menu_series SET version=version+1 WHERE id=?",
            (before.series.id,),
        )
    return store.get_weekly_menu_revision(context, revision_id)
