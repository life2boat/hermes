from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WeeklyMenuMemberGenerationSnapshot:
    age_band: str | None = None
    daily_kcal_target: float | None = None
    daily_protein_g: float | None = None
    daily_fat_g: float | None = None
    daily_carbs_g: float | None = None
    dietary_notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WeeklyMenuGenerationRequest:
    week_start: str
    dates: tuple[str, ...]
    allowed_meal_slots: tuple[str, ...]
    locale: str
    member_count: int
    members: tuple[WeeklyMenuMemberGenerationSnapshot, ...]
    household_dietary_notes: tuple[str, ...]
    max_entries: int


@dataclass(frozen=True, slots=True)
class WeeklyMenuGeneratedEntry:
    local_date: str
    meal_slot: str
    position: int
    title: str
    description: str | None = None
    servings: str | None = None


@dataclass(frozen=True, slots=True)
class WeeklyMenuGenerationResponse:
    entries: tuple[WeeklyMenuGeneratedEntry, ...]
