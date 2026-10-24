"""
gateway.nutrition.user_override — Typed contracts for user-supplied nutrition overrides
and structured action plans.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class NutritionBasis(str, Enum):
    TOTAL_PORTION = "TOTAL_PORTION"
    PER_100G = "PER_100G"
    PER_ITEM = "PER_ITEM"


class ActionOperation(str, Enum):
    REPLACE_ITEM = "REPLACE_ITEM"
    UPDATE_WEIGHT = "UPDATE_WEIGHT"
    UPDATE_QUANTITY = "UPDATE_QUANTITY"
    SET_NUTRITION_VALUES = "SET_NUTRITION_VALUES"
    SET_USER_NUTRITION = "SET_USER_NUTRITION"
    ADD_ITEM = "ADD_ITEM"
    REMOVE_ITEM = "REMOVE_ITEM"
    SCALE_PORTION = "SCALE_PORTION"
    RECALCULATE = "RECALCULATE"
    REQUEST_CLARIFICATION = "REQUEST_CLARIFICATION"
    REQUEST_SAVE = "REQUEST_SAVE"


@dataclass
class UserNutritionOverride:
    item_id: str = ""
    grams: float | None = None
    calories_kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None
    nutrition_basis: str = "TOTAL_PORTION"  # TOTAL_PORTION, PER_100G, PER_ITEM
    provenance: str = "USER_SUPPLIED"
    explicitly_confirmed: bool = True
    updated_at: str | None = None
    inventory_version: int = 1

    def __init__(
        self,
        item_id: str = "",
        grams: float | None = None,
        calories_kcal: float | None = None,
        protein_g: float | None = None,
        fat_g: float | None = None,
        carbs_g: float | None = None,
        nutrition_basis: str | NutritionBasis | None = None,
        provenance: str = "USER_SUPPLIED",
        explicitly_confirmed: bool = True,
        updated_at: str | None = None,
        inventory_version: int = 1,
        *,
        basis: str | NutritionBasis | None = None,
    ) -> None:
        self.item_id = item_id
        self.grams = grams
        self.calories_kcal = calories_kcal
        self.protein_g = protein_g
        self.fat_g = fat_g
        self.carbs_g = carbs_g
        chosen_basis = basis or nutrition_basis or "TOTAL_PORTION"
        if isinstance(chosen_basis, Enum):
            chosen_basis = chosen_basis.value
        self.nutrition_basis = str(chosen_basis)
        self.provenance = provenance
        self.explicitly_confirmed = explicitly_confirmed
        self.updated_at = updated_at
        self.inventory_version = inventory_version

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "grams": self.grams,
            "calories_kcal": self.calories_kcal,
            "protein_g": self.protein_g,
            "fat_g": self.fat_g,
            "carbs_g": self.carbs_g,
            "nutrition_basis": self.nutrition_basis,
            "provenance": self.provenance,
            "explicitly_confirmed": self.explicitly_confirmed,
            "updated_at": self.updated_at,
            "inventory_version": self.inventory_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> UserNutritionOverride | None:
        if not data or not isinstance(data, dict):
            return None
        return cls(
            item_id=str(data.get("item_id") or ""),
            grams=float(data["grams"]) if data.get("grams") is not None else None,
            calories_kcal=float(data["calories_kcal"]) if data.get("calories_kcal") is not None else None,
            protein_g=float(data["protein_g"]) if data.get("protein_g") is not None else None,
            fat_g=float(data["fat_g"]) if data.get("fat_g") is not None else None,
            carbs_g=float(data["carbs_g"]) if data.get("carbs_g") is not None else None,
            nutrition_basis=str(data.get("nutrition_basis") or data.get("basis") or "TOTAL_PORTION"),
            provenance=str(data.get("provenance") or "USER_SUPPLIED"),
            explicitly_confirmed=bool(data.get("explicitly_confirmed", True)),
            updated_at=data.get("updated_at"),
            inventory_version=int(data.get("inventory_version") or 1),
        )


@dataclass
class StructuredAction:
    operation: ActionOperation
    target_index: int | None = None
    target_name: str | None = None
    target_item_id: str | None = None
    name: str | None = None
    grams: float | None = None
    quantity: float | None = None
    scale_factor: float | None = None
    user_nutrition: UserNutritionOverride | None = None
    confidence: float = 1.0
    provenance: str = "DETERMINISTIC"

    def __init__(
        self,
        operation: ActionOperation,
        target_index: int | None = None,
        target_name: str | None = None,
        target_item_id: str | None = None,
        name: str | None = None,
        grams: float | None = None,
        quantity: float | None = None,
        scale_factor: float | None = None,
        user_nutrition: UserNutritionOverride | None = None,
        confidence: float = 1.0,
        provenance: str = "DETERMINISTIC",
        *,
        replacement_name: str | None = None,
        new_weight_g: float | None = None,
    ) -> None:
        self.operation = operation
        self.target_index = target_index
        self.target_name = target_name
        self.target_item_id = target_item_id
        self.name = replacement_name if replacement_name is not None else name
        self.grams = new_weight_g if new_weight_g is not None else grams
        self.quantity = quantity
        self.scale_factor = scale_factor
        self.user_nutrition = user_nutrition
        self.confidence = confidence
        self.provenance = provenance


@dataclass(slots=True)
class StructuredActionPlan:
    inventory_id: str = ""
    inventory_version: int = 1
    user_id: int = 0
    actions: list[StructuredAction] = field(default_factory=list)
    confidence: float = 1.0
    requires_clarification: bool = False
    clarification_question: str | None = None
    raw_user_message: str = ""
