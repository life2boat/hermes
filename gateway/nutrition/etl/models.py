"""
gateway.nutrition.etl.models — Data structures for the nutrition ETL pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ETLFoodItem:
    food_id: str
    name_ru: str
    name_en: str
    category: str
    cooking_state: str
    source_dataset: str
    source_id: str
    dataset_version: str
    source_provenance: str
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float
    fiber_g: float = 0.0
    sugars_g: float = 0.0
    reference_confidence: float = 0.90
    variance_note: str = ""
    aliases: tuple[str, ...] = ()
    portions: tuple[tuple[str, float], ...] = ()
