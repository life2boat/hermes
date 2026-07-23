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
class WeeklyMenuInventoryItem:
    normalized_name: str
    display_name: str
    quantity_value: str | None
    unit: str
    category: str | None = None


@dataclass(frozen=True, slots=True)
class WeeklyMenuIngredient:
    name: str
    quantity_value: str
    unit: str


@dataclass(frozen=True, slots=True)
class WeeklyMenuMacros:
    protein_g: str
    carbs_g: str
    fat_g: str


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
    inventory_snapshot_id: str | None = None
    inventory_items: tuple[WeeklyMenuInventoryItem, ...] = ()
    inventory_only: bool = False


@dataclass(frozen=True, slots=True)
class WeeklyMenuGeneratedEntry:
    local_date: str
    meal_slot: str
    position: int
    title: str
    description: str | None = None
    servings: str | None = None
    instructions: tuple[str, ...] = ()
    estimated_calories_per_serving: int | None = None
    macros_per_serving: WeeklyMenuMacros | None = None
    ingredients: tuple[WeeklyMenuIngredient, ...] = ()


@dataclass(frozen=True, slots=True)
class WeeklyMenuGenerationResponse:
    entries: tuple[WeeklyMenuGeneratedEntry, ...]
