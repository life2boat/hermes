"""
gateway.nutrition.contracts — Shared contracts and domain primitives for Nutrition Intelligence v2.2.

Provides normative contracts for:
1. Uncertainty Calculation (Conservative Interval, Calibrated Quantile, Unresolved).
2. Plausible range labeling (approximate plausible range vs guaranteed physical interval).
3. Recipe mass constraints and cooking yield consistency.
4. Shared visual scale uncertainty across distinct components in the same scene.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Sequence


class UncertaintyModelType(str, Enum):
    CONSERVATIVE_INTERVAL = "CONSERVATIVE_INTERVAL"
    CALIBRATED_QUANTILE = "CALIBRATED_QUANTILE"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class NutrientInterval:
    """
    Closed interval [min_value, max_value] with a central estimate center_value.
    Invariant: 0.0 <= min_value <= center_value <= max_value (within numerical tolerance).
    """
    min_value: float
    center_value: float
    max_value: float

    def __post_init__(self) -> None:
        if self.min_value < -1e-6:
            raise ValueError(f"Nutrient interval min_value must be non-negative, got {self.min_value}")
        # Clamp tiny floating-point negatives to 0.0
        clamped_min = max(0.0, float(self.min_value))
        clamped_center = max(clamped_min, float(self.center_value))
        clamped_max = max(clamped_center, float(self.max_value))

        object.__setattr__(self, "min_value", round(clamped_min, 4))
        object.__setattr__(self, "center_value", round(clamped_center, 4))
        object.__setattr__(self, "max_value", round(clamped_max, 4))

    @property
    def span(self) -> float:
        return self.max_value - self.min_value

    @property
    def is_point(self) -> bool:
        return abs(self.max_value - self.min_value) < 1e-6

    def scale(self, factor: float) -> NutrientInterval:
        if factor < 0.0:
            raise ValueError(f"Scale factor must be non-negative, got {factor}")
        return NutrientInterval(
            min_value=self.min_value * factor,
            center_value=self.center_value * factor,
            max_value=self.max_value * factor,
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "min": self.min_value,
            "center": self.center_value,
            "max": self.max_value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, float]) -> NutrientInterval:
        return cls(
            min_value=float(data["min"]),
            center_value=float(data["center"]),
            max_value=float(data["max"]),
        )


@dataclass(frozen=True, slots=True)
class MacronutrientIntervals:
    """Set of nutrient intervals for calories, protein, fat, and carbohydrates."""
    calories_kcal: NutrientInterval | None = None
    protein_g: NutrientInterval | None = None
    fat_g: NutrientInterval | None = None
    carbs_g: NutrientInterval | None = None

    def is_empty(self) -> bool:
        return (
            self.calories_kcal is None
            and self.protein_g is None
            and self.fat_g is None
            and self.carbs_g is None
        )

    def to_dict(self) -> dict[str, dict[str, float] | None]:
        return {
            "calories_kcal": self.calories_kcal.to_dict() if self.calories_kcal else None,
            "protein_g": self.protein_g.to_dict() if self.protein_g else None,
            "fat_g": self.fat_g.to_dict() if self.fat_g else None,
            "carbs_g": self.carbs_g.to_dict() if self.carbs_g else None,
        }


@dataclass(frozen=True, slots=True)
class MassBounds:
    """
    Mass bounds for a food component.
    - grams_min: Lower estimated mass bound (g)
    - grams_center: Point or median mass estimate (g)
    - grams_max: Upper estimated mass bound (g)
    - is_approximate_plausible_range: True when mass bounds are model-estimated rather
      than physically weighed; indicates an approximate plausible range, not a guaranteed physical interval.
    - is_user_confirmed: True if the user manually provided or confirmed the mass.
    """
    grams_min: float
    grams_center: float
    grams_max: float
    is_approximate_plausible_range: bool = True
    is_user_confirmed: bool = False

    def __post_init__(self) -> None:
        if self.grams_min < 0.0:
            raise ValueError(f"grams_min must be >= 0.0, got {self.grams_min}")
        clamped_min = max(0.0, float(self.grams_min))
        clamped_center = max(clamped_min, float(self.grams_center))
        clamped_max = max(clamped_center, float(self.grams_max))

        object.__setattr__(self, "grams_min", round(clamped_min, 2))
        object.__setattr__(self, "grams_center", round(clamped_center, 2))
        object.__setattr__(self, "grams_max", round(clamped_max, 2))

    @property
    def span(self) -> float:
        return self.grams_max - self.grams_min


@dataclass(frozen=True, slots=True)
class CalibratedQuantileSpec:
    """Calibrated quantile specification for probability-bound nutrition calculations."""
    q10: float
    q50: float
    q90: float
    calibration_basis: str = ""

    def __post_init__(self) -> None:
        if not (self.q10 <= self.q50 <= self.q90):
            raise ValueError(f"Quantiles must satisfy q10 <= q50 <= q90, got ({self.q10}, {self.q50}, {self.q90})")


@dataclass(frozen=True, slots=True)
class SharedVisualScaleUncertainty:
    """
    Shared visual scale factor uncertainty.
    Components identified in the same photograph share scale errors arising from camera angle,
    perspective tilt, and unknown plate/vessel dimensions; portion mass estimates therefore
    cannot be treated as mutually independent random variables.
    """
    scale_factor_min: float = 0.85
    scale_factor_center: float = 1.00
    scale_factor_max: float = 1.15
    is_shared_across_components: bool = True

    def __post_init__(self) -> None:
        if not (0.0 < self.scale_factor_min <= self.scale_factor_center <= self.scale_factor_max):
            raise ValueError(
                f"Scale factors must satisfy 0 < min <= center <= max, "
                f"got ({self.scale_factor_min}, {self.scale_factor_center}, {self.scale_factor_max})"
            )

    def apply_to_mass(self, mass: MassBounds) -> MassBounds:
        """Scales a mass interval by the shared visual scale factor."""
        return MassBounds(
            grams_min=round(mass.grams_min * self.scale_factor_min, 2),
            grams_center=round(mass.grams_center * self.scale_factor_center, 2),
            grams_max=round(mass.grams_max * self.scale_factor_max, 2),
            is_approximate_plausible_range=True,
            is_user_confirmed=mass.is_user_confirmed,
        )


@dataclass(frozen=True, slots=True)
class RecipeMassConstraint:
    """
    Recipe mass constraints & cooking yield consistency contract.
    - total_dish_mass_min_g: Minimum plausible total cooked dish weight.
    - total_dish_mass_max_g: Maximum plausible total cooked dish weight.
    - cooking_yield_factor: Ratio of cooked dish weight to raw ingredients weight
      (e.g. 0.75 for roasted meat due to moisture loss; 2.5 for boiled rice due to hydration).
    - recipe_ingredient_names: Set of normalized names belonging to the recipe.
    - external_ingredient_names: Set of normalized names for side dishes/toppings outside the recipe.
    """
    total_dish_mass_min_g: float | None = None
    total_dish_mass_max_g: float | None = None
    cooking_yield_factor: float = 1.0
    recipe_ingredient_names: frozenset[str] = field(default_factory=frozenset)
    external_ingredient_names: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.cooking_yield_factor <= 0.0:
            raise ValueError(f"cooking_yield_factor must be positive, got {self.cooking_yield_factor}")
        if (
            self.total_dish_mass_min_g is not None
            and self.total_dish_mass_max_g is not None
            and self.total_dish_mass_min_g > self.total_dish_mass_max_g
        ):
            raise ValueError("total_dish_mass_min_g cannot exceed total_dish_mass_max_g")

    def raw_to_cooked_mass(self, raw_mass_g: float) -> float:
        return raw_mass_g * self.cooking_yield_factor

    def cooked_to_raw_mass(self, cooked_mass_g: float) -> float:
        return cooked_mass_g / self.cooking_yield_factor

    def validate_mass_balance(
        self,
        raw_ingredients_mass_sum: float,
        cooked_dish_mass: float,
        tolerance: float = 0.15,
    ) -> bool:
        """
        Validates that cooked dish mass is consistent with sum of raw ingredients scaled by cooking yield.
        Returns True if within relative tolerance.
        """
        expected_cooked = self.raw_to_cooked_mass(raw_ingredients_mass_sum)
        if expected_cooked <= 0.0:
            return cooked_dish_mass <= 0.0
        diff_ratio = abs(cooked_dish_mass - expected_cooked) / expected_cooked
        return diff_ratio <= tolerance

    def clamp_total_dish_mass(self, proposed_dish_mass_g: float) -> float:
        """Clamps a cooked dish mass within the recipe limits if specified."""
        mass = proposed_dish_mass_g
        if self.total_dish_mass_min_g is not None:
            mass = max(self.total_dish_mass_min_g, mass)
        if self.total_dish_mass_max_g is not None:
            mass = min(self.total_dish_mass_max_g, mass)
        return mass


@dataclass(frozen=True, slots=True)
class ComponentNutritionEstimate:
    """Nutritional calculation result for an individual food component."""
    name: str
    mass_bounds: MassBounds
    intervals: MacronutrientIntervals
    source_id: str = ""
    cooking_state: str = "raw"
    source_provenance: str = ""
    per_100g_calories_kcal: float | None = None
    per_100g_protein_g: float | None = None
    per_100g_fat_g: float | None = None
    per_100g_carbs_g: float | None = None
    uncertainty_model_type: UncertaintyModelType = UncertaintyModelType.CONSERVATIVE_INTERVAL
    is_approximate_plausible_range: bool = True
    nutrition_available: bool = True
    candidate_suggestions: tuple[str, ...] = ()
    variance_note: str = ""


@dataclass(frozen=True, slots=True)
class MealNutritionEstimate:
    """Aggregated nutritional calculation result for a full meal or plate."""
    meal_name: str
    components: tuple[ComponentNutritionEstimate, ...]
    total_intervals: MacronutrientIntervals
    uncertainty_model_type: UncertaintyModelType
    is_approximate_plausible_range: bool
    recipe_mass_constraint: RecipeMassConstraint | None = None
    shared_visual_scale_uncertainty: SharedVisualScaleUncertainty | None = None
    has_unresolved_components: bool = False
    overall_confidence: float = 1.0


# -----------------------------------------------------------------------------
# Conservative Interval Arithmetic Core Functions
# -----------------------------------------------------------------------------

def calculate_conservative_nutrient_interval(
    mass_min: float,
    mass_center: float,
    mass_max: float,
    nutrient_per_100g: float,
) -> NutrientInterval:
    """
    Deterministic calculation for nonnegative nutrient quantities where only lower and upper bounds are available:
    V_center = m_center * n_center / 100
    V_min = m_min * n_min / 100
    V_max = m_max * n_max / 100
    """
    if nutrient_per_100g < 0.0:
        raise ValueError(f"nutrient_per_100g must be non-negative, got {nutrient_per_100g}")

    factor = nutrient_per_100g / 100.0
    v_min = max(0.0, mass_min * factor)
    v_center = max(v_min, mass_center * factor)
    v_max = max(v_center, mass_max * factor)

    return NutrientInterval(
        min_value=v_min,
        center_value=v_center,
        max_value=v_max,
    )


def aggregate_conservative_intervals(
    intervals: Sequence[NutrientInterval | None],
) -> NutrientInterval | None:
    """
    Aggregates several distinct components under conservative interval arithmetic:
    V_total_center = sum(V_center_i)
    V_total_min = sum(V_min_i)
    V_total_max = sum(V_max_i)
    """
    valid_intervals = [item for item in intervals if item is not None]
    if not valid_intervals:
        return None

    total_min = sum(item.min_value for item in valid_intervals)
    total_center = sum(item.center_value for item in valid_intervals)
    total_max = sum(item.max_value for item in valid_intervals)

    return NutrientInterval(
        min_value=total_min,
        center_value=total_center,
        max_value=total_max,
    )


def calculate_component_macronutrient_intervals(
    mass_bounds: MassBounds,
    calories_per_100g: float | None,
    protein_per_100g: float | None,
    fat_per_100g: float | None,
    carbs_per_100g: float | None,
) -> MacronutrientIntervals:
    """Computes conservative nutrient intervals for all 4 macronutrients given mass bounds and per-100g profile."""
    cal_interval = (
        calculate_conservative_nutrient_interval(
            mass_bounds.grams_min,
            mass_bounds.grams_center,
            mass_bounds.grams_max,
            calories_per_100g,
        )
        if calories_per_100g is not None
        else None
    )
    prot_interval = (
        calculate_conservative_nutrient_interval(
            mass_bounds.grams_min,
            mass_bounds.grams_center,
            mass_bounds.grams_max,
            protein_per_100g,
        )
        if protein_per_100g is not None
        else None
    )
    fat_interval = (
        calculate_conservative_nutrient_interval(
            mass_bounds.grams_min,
            mass_bounds.grams_center,
            mass_bounds.grams_max,
            fat_per_100g,
        )
        if fat_per_100g is not None
        else None
    )
    carbs_interval = (
        calculate_conservative_nutrient_interval(
            mass_bounds.grams_min,
            mass_bounds.grams_center,
            mass_bounds.grams_max,
            carbs_per_100g,
        )
        if carbs_per_100g is not None
        else None
    )

    return MacronutrientIntervals(
        calories_kcal=cal_interval,
        protein_g=prot_interval,
        fat_g=fat_interval,
        carbs_g=carbs_interval,
    )
