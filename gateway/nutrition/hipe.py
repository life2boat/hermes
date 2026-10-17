"""
gateway.nutrition.hipe — HealBite Intelligent Portion Estimator (HIPE).

Provides:
1. Volume-to-density conversion tables (density in g/cm³ or g/ml) across food categories.
2. Visual portion priors for Russian and standard dish components.
3. Visual anchor scale references (dinner plate, dessert plate, bowl, glass, etc.).
4. Deterministic mass bounds estimation with conservative widening when anchors are absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Mapping

from gateway.nutrition.contracts import (
    MassBounds,
    SharedVisualScaleUncertainty,
)
from gateway.nutrition.search import normalize_russian_food_query


class VisualAnchorType(str, Enum):
    """Visual reference objects used for visual scale calibration in meal photos."""
    STANDARD_DINNER_PLATE = "STANDARD_DINNER_PLATE"   # ~25-27 cm diameter
    DESSERT_PLATE = "DESSERT_PLATE"                   # ~18-20 cm diameter
    SOUP_BOWL = "SOUP_BOWL"                           # ~14-16 cm, 400-500 ml volume
    MUG_CUP = "MUG_CUP"                               # ~250-350 ml volume
    WATER_GLASS = "WATER_GLASS"                       # ~200-250 ml volume
    CUTLERY_FORK_SPOON = "CUTLERY_FORK_SPOON"         # ~18-20 cm length
    HAND_PALM = "HAND_PALM"                           # ~80-120 cm² surface area
    NONE = "NONE"                                     # No reference object detected


@dataclass(frozen=True, slots=True)
class FoodDensity:
    """Density bounds in g/cm³ (or g/ml). Invariant: 0 < min <= center <= max."""
    density_min: float
    density_center: float
    density_max: float
    category: str

    def __post_init__(self) -> None:
        if not (0.0 < self.density_min <= self.density_center <= self.density_max):
            raise ValueError(
                f"Density must satisfy 0 < min <= center <= max, "
                f"got ({self.density_min}, {self.density_center}, {self.density_max})"
            )


@dataclass(frozen=True, slots=True)
class PortionPrior:
    """Portion mass prior in grams. Invariant: 0 < grams_min <= grams_center <= grams_max."""
    grams_min: float
    grams_center: float
    grams_max: float
    category: str
    typical_unit: str = "порция"

    def __post_init__(self) -> None:
        if not (0.0 < self.grams_min <= self.grams_center <= self.grams_max):
            raise ValueError(
                f"Portion prior must satisfy 0 < min <= center <= max, "
                f"got ({self.grams_min}, {self.grams_center}, {self.grams_max})"
            )


# -----------------------------------------------------------------------------
# Food Density Table
# -----------------------------------------------------------------------------

_DENSITY_TABLE: dict[str, FoodDensity] = {
    "soup": FoodDensity(0.95, 1.00, 1.05, category="soup"),
    "porridge": FoodDensity(0.70, 0.80, 0.90, category="porridge"),
    "pasta": FoodDensity(0.65, 0.75, 0.85, category="pasta"),
    "meat": FoodDensity(0.85, 0.95, 1.10, category="meat"),
    "poultry": FoodDensity(0.85, 0.95, 1.08, category="poultry"),
    "fish": FoodDensity(0.80, 0.92, 1.05, category="fish"),
    "dairy_dense": FoodDensity(0.65, 0.85, 1.10, category="dairy_dense"),
    "dairy_liquid": FoodDensity(1.00, 1.03, 1.05, category="dairy_liquid"),
    "salad_leafy": FoodDensity(0.20, 0.35, 0.50, category="salad_leafy"),
    "salad_dense": FoodDensity(0.65, 0.80, 0.95, category="salad_dense"),
    "vegetable_cooked": FoodDensity(0.65, 0.80, 0.95, category="vegetable_cooked"),
    "vegetable_raw": FoodDensity(0.50, 0.65, 0.80, category="vegetable_raw"),
    "beverage": FoodDensity(1.00, 1.00, 1.04, category="beverage"),
    "bread": FoodDensity(0.25, 0.35, 0.45, category="bread"),
    "sauce": FoodDensity(0.85, 0.95, 1.05, category="sauce"),
    "fruit": FoodDensity(0.55, 0.70, 0.85, category="fruit"),
    "nuts": FoodDensity(0.50, 0.65, 0.75, category="nuts"),
    "dessert": FoodDensity(0.40, 0.60, 0.85, category="dessert"),
    "default": FoodDensity(0.60, 0.80, 1.00, category="default"),
}

# Mapping patterns to density categories
_DENSITY_CATEGORY_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(суп|борщ|щи|бульон|солянк|рассольник|уха|окрошк|харчо|гаспачо)"), "soup"),
    (re.compile(r"(каш|гречк|рис|овсянк|пшен|булгур|киноа|перловк|кускус)"), "porridge"),
    (re.compile(r"(макарон|паст|спагетти|лапш|вермишел|фетучини|пенне)"), "pasta"),
    (re.compile(r"(говядин|свинин|телятин|баранин|фарш|гуляш|котлет|стейк|бифштекс|отбивн)"), "meat"),
    (re.compile(r"(куриц|индейк|цыпленок|филе куриное|грудк|крылышк|бедро)"), "poultry"),
    (re.compile(r"(рыб|лосос|семг|форел|треск|тунец|минтай|судак|окунь|хек|сельдь)"), "fish"),
    (re.compile(r"(творог|сыр|маскарпоне|рикотта|моцарелла|сулугуни|брынза|сметан)"), "dairy_dense"),
    (re.compile(r"(молок|кефир|ряженк|йогурт|сливк|простокваш)"), "dairy_liquid"),
    (re.compile(r"(руккол|шпинат|айсберг|латук|листья салата|зелень|петрушк|укроп)"), "salad_leafy"),
    (re.compile(r"(оливье|винегрет|цезарь|крабовый|греческий салат|селедка под шубой)"), "salad_dense"),
    (re.compile(r"(рагу|пюре|тушен|жарен|запечен|овощное соте|кабачк|баклажан|брокколи)"), "vegetable_cooked"),
    (re.compile(r"(огурец|помидор|томат|перец болгарский|морковь|редис|капуст)"), "vegetable_raw"),
    (re.compile(r"(чай|кофе|сок|компот|морс|вода|лимонад|напиток|квас)"), "beverage"),
    (re.compile(r"(хлеб|батон|булк|багет|лаваш|круассан|тост|сухар)"), "bread"),
    (re.compile(r"(соус|майонез|кетчуп|песто|масло|заправк)"), "sauce"),
    (re.compile(r"(яблок|груш|банан|апельсин|мандарин|ягод|клубник|малин|черник|виноград)"), "fruit"),
    (re.compile(r"(орех|миндаль|фундук|грецкий|кешью|семечк)"), "nuts"),
    (re.compile(r"(торт|пирожн|кекс|печень|печенье|шоколад|конфет|блин|сырник)"), "dessert"),
)


def get_food_density(food_name: str, explicit_category: str | None = None) -> FoodDensity:
    """Returns the density specification (g/cm³) for the given food name or category."""
    if explicit_category and explicit_category in _DENSITY_TABLE:
        return _DENSITY_TABLE[explicit_category]

    query = normalize_russian_food_query(food_name)
    for pattern, cat in _DENSITY_CATEGORY_RULES:
        if pattern.search(query):
            return _DENSITY_TABLE[cat]

    return _DENSITY_TABLE["default"]


# -----------------------------------------------------------------------------
# Visual Portion Priors
# -----------------------------------------------------------------------------

_PORTION_PRIORS_TABLE: dict[str, PortionPrior] = {
    "soup": PortionPrior(200.0, 300.0, 450.0, category="soup", typical_unit="глубокая тарелка"),
    "porridge": PortionPrior(120.0, 200.0, 300.0, category="porridge", typical_unit="порция"),
    "pasta": PortionPrior(120.0, 180.0, 280.0, category="pasta", typical_unit="порция"),
    "meat_cutlet": PortionPrior(70.0, 100.0, 150.0, category="meat_cutlet", typical_unit="штука"),
    "meat_steak": PortionPrior(120.0, 180.0, 260.0, category="meat_steak", typical_unit="кусок"),
    "poultry_breast": PortionPrior(100.0, 150.0, 220.0, category="poultry_breast", typical_unit="кусок"),
    "fish_portion": PortionPrior(100.0, 150.0, 220.0, category="fish_portion", typical_unit="кусок"),
    "salad_fresh": PortionPrior(80.0, 150.0, 250.0, category="salad_fresh", typical_unit="порция"),
    "vegetable_side": PortionPrior(100.0, 150.0, 250.0, category="vegetable_side", typical_unit="порция"),
    "bread_slice": PortionPrior(25.0, 35.0, 50.0, category="bread_slice", typical_unit="кусок"),
    "tvorog_serving": PortionPrior(100.0, 150.0, 250.0, category="tvorog_serving", typical_unit="порция"),
    "cheese_slice": PortionPrior(20.0, 30.0, 45.0, category="cheese_slice", typical_unit="ломтик"),
    "egg_piece": PortionPrior(45.0, 55.0, 65.0, category="egg_piece", typical_unit="штука"),
    "beverage_mug": PortionPrior(180.0, 250.0, 350.0, category="beverage_mug", typical_unit="чашка"),
    "beverage_glass": PortionPrior(150.0, 200.0, 280.0, category="beverage_glass", typical_unit="стакан"),
    "sauce_spoon": PortionPrior(15.0, 25.0, 40.0, category="sauce_spoon", typical_unit="столовая ложка"),
    "fruit_piece": PortionPrior(80.0, 150.0, 220.0, category="fruit_piece", typical_unit="штука"),
    "dessert_pastry": PortionPrior(60.0, 100.0, 160.0, category="dessert_pastry", typical_unit="порция"),
    "syrniki": PortionPrior(100.0, 160.0, 240.0, category="сырники", typical_unit="порция (2-3 шт)"),
    "omelette": PortionPrior(100.0, 150.0, 220.0, category="omelette", typical_unit="порция"),
    "oil_butter": PortionPrior(5.0, 15.0, 30.0, category="oil_butter", typical_unit="порция"),
    "vegetable_cut": PortionPrior(40.0, 90.0, 160.0, category="vegetable_cut", typical_unit="порция"),
    "pilaf": PortionPrior(180.0, 250.0, 350.0, category="pilaf", typical_unit="порция"),
    "salad_dense": PortionPrior(120.0, 180.0, 280.0, category="salad_dense", typical_unit="порция"),
    "casserole": PortionPrior(150.0, 220.0, 320.0, category="casserole", typical_unit="порция"),
    "pancakes": PortionPrior(80.0, 140.0, 220.0, category="pancakes", typical_unit="порция (2-3 шт)"),
    "cutlet": PortionPrior(70.0, 100.0, 150.0, category="meat_cutlet", typical_unit="штука"),
    "meat_cutlet": PortionPrior(70.0, 100.0, 150.0, category="meat_cutlet", typical_unit="штука"),
    "default": PortionPrior(100.0, 180.0, 300.0, category="default", typical_unit="порция"),
}

_PRIOR_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(суп|борщ|щи|бульон|солянк|рассольник|уха|горохов.*суп|харчо|окрошк)"), "soup"),
    (re.compile(r"(сырник)"), "syrniki"),
    (re.compile(r"(омлет|яичниц|глазунь)"), "omelette"),
    (re.compile(r"(котлет|тефтел|биточк|фрикадельк)"), "meat_cutlet"),
    (re.compile(r"(плов)"), "pilaf"),
    (re.compile(r"(запеканк)"), "casserole"),
    (re.compile(r"(блин|блинчик|оладь)"), "pancakes"),
    (re.compile(r"(оливье|сельд.*под.*шуб|селедк.*под.*шуб|крабов.*салат|мимоз)"), "salad_dense"),
    (re.compile(r"(стейк|отбивн|гуляш|шашлык|эскалоп|бифштекс|жарко)"), "meat_steak"),
    (re.compile(r"(куриц|индейк|грудк|филе|бедро)"), "poultry_breast"),
    (re.compile(r"(рыб|лосос|семг|форел|судак|минтай|хек|треск)"), "fish_portion"),
    (re.compile(r"(макарон|паст|спагетти|лапш)"), "pasta"),
    (re.compile(r"(каш|гречк|рис|овсянк|булгур|перловк)"), "porridge"),
    (re.compile(r"(салат|огурц.*помидор|винегрет|греческий|цезар)"), "salad_fresh"),
    (re.compile(r"(рагу|пюре|тушен.*овощ|кабачк|брокколи)"), "vegetable_side"),
    (re.compile(r"(хлеб|булк|батон|тост|кусок хлеба)"), "bread_slice"),
    (re.compile(r"(творог|пачка творога)"), "tvorog_serving"),
    (re.compile(r"(сыр|ломтик сыра)"), "cheese_slice"),
    (re.compile(r"(яйц)"), "egg_piece"),
    (re.compile(r"(масл|сливочн.*масл|растительн.*масл|оливков.*масл|масло)"), "oil_butter"),
    (re.compile(r"(огур|помидор|томат|перец)"), "vegetable_cut"),
    (re.compile(r"(чай|кофе|кружк|чашк)"), "beverage_mug"),
    (re.compile(r"(сок|стакан|вод|компот|морс|молок|кефир)"), "beverage_glass"),
    (re.compile(r"(соус|майонез|кетчуп|ложк|сметан.*ложк)"), "sauce_spoon"),
    (re.compile(r"(яблок|груш|банан|апельсин)"), "fruit_piece"),
    (re.compile(r"(торт|пирожн|десерт|круассан|булочк|пирог|пирожок|пирожк)"), "dessert_pastry"),
)


def get_portion_prior(food_name: str, explicit_category: str | None = None) -> PortionPrior:
    """Returns the portion mass prior (g) for the given food name or category."""
    if explicit_category and explicit_category in _PORTION_PRIORS_TABLE:
        return _PORTION_PRIORS_TABLE[explicit_category]

    query = normalize_russian_food_query(food_name)
    for pattern, prior_key in _PRIOR_RULES:
        if pattern.search(query):
            return _PORTION_PRIORS_TABLE[prior_key]

    return _PORTION_PRIORS_TABLE["default"]


# -----------------------------------------------------------------------------
# Visual Anchor Calibration
# -----------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class VisualAnchorSpec:
    """Specification of a visual scale anchor and its associated uncertainty."""
    anchor_type: VisualAnchorType
    uncertainty_multiplier_min: float
    uncertainty_multiplier_max: float
    shared_scale: SharedVisualScaleUncertainty


_ANCHOR_SPECS: Mapping[VisualAnchorType, VisualAnchorSpec] = {
    VisualAnchorType.STANDARD_DINNER_PLATE: VisualAnchorSpec(
        anchor_type=VisualAnchorType.STANDARD_DINNER_PLATE,
        uncertainty_multiplier_min=0.88,
        uncertainty_multiplier_max=1.12,
        shared_scale=SharedVisualScaleUncertainty(0.88, 1.00, 1.12),
    ),
    VisualAnchorType.DESSERT_PLATE: VisualAnchorSpec(
        anchor_type=VisualAnchorType.DESSERT_PLATE,
        uncertainty_multiplier_min=0.85,
        uncertainty_multiplier_max=1.15,
        shared_scale=SharedVisualScaleUncertainty(0.85, 1.00, 1.15),
    ),
    VisualAnchorType.SOUP_BOWL: VisualAnchorSpec(
        anchor_type=VisualAnchorType.SOUP_BOWL,
        uncertainty_multiplier_min=0.85,
        uncertainty_multiplier_max=1.15,
        shared_scale=SharedVisualScaleUncertainty(0.85, 1.00, 1.15),
    ),
    VisualAnchorType.MUG_CUP: VisualAnchorSpec(
        anchor_type=VisualAnchorType.MUG_CUP,
        uncertainty_multiplier_min=0.90,
        uncertainty_multiplier_max=1.10,
        shared_scale=SharedVisualScaleUncertainty(0.90, 1.00, 1.10),
    ),
    VisualAnchorType.WATER_GLASS: VisualAnchorSpec(
        anchor_type=VisualAnchorType.WATER_GLASS,
        uncertainty_multiplier_min=0.90,
        uncertainty_multiplier_max=1.10,
        shared_scale=SharedVisualScaleUncertainty(0.90, 1.00, 1.10),
    ),
    VisualAnchorType.CUTLERY_FORK_SPOON: VisualAnchorSpec(
        anchor_type=VisualAnchorType.CUTLERY_FORK_SPOON,
        uncertainty_multiplier_min=0.82,
        uncertainty_multiplier_max=1.18,
        shared_scale=SharedVisualScaleUncertainty(0.82, 1.00, 1.18),
    ),
    VisualAnchorType.HAND_PALM: VisualAnchorSpec(
        anchor_type=VisualAnchorType.HAND_PALM,
        uncertainty_multiplier_min=0.80,
        uncertainty_multiplier_max=1.20,
        shared_scale=SharedVisualScaleUncertainty(0.80, 1.00, 1.20),
    ),
    VisualAnchorType.NONE: VisualAnchorSpec(
        anchor_type=VisualAnchorType.NONE,
        # Wider conservative bounds when no physical scale object is available
        uncertainty_multiplier_min=0.70,
        uncertainty_multiplier_max=1.35,
        shared_scale=SharedVisualScaleUncertainty(0.70, 1.00, 1.35),
    ),
}


def get_anchor_spec(anchor: VisualAnchorType) -> VisualAnchorSpec:
    return _ANCHOR_SPECS.get(anchor, _ANCHOR_SPECS[VisualAnchorType.NONE])


# -----------------------------------------------------------------------------
# Core HIPE Portion Estimator Function
# -----------------------------------------------------------------------------

def estimate_portion_mass(
    food_name: str,
    *,
    visual_anchor: VisualAnchorType = VisualAnchorType.NONE,
    visual_volume_cm3: float | None = None,
    detected_count: float = 1.0,
    user_confirmed_grams: float | None = None,
    explicit_category: str | None = None,
) -> MassBounds:
    """
    Estimates conservative mass bounds [grams_min, grams_center, grams_max] for a component.

    Rules:
    1. If user_confirmed_grams is provided, returns exact point bounds (min == center == max),
       marked with is_approximate_plausible_range=False and is_user_confirmed=True.
    2. If visual_volume_cm3 is provided, mass is derived via density [m = V * rho] and
       scaled according to the visual anchor uncertainty.
    3. If only food_name is provided, uses category PortionPrior multiplied by detected_count
       and adjusted for anchor presence/absence. When visual_anchor is NONE, uncertainty is
       conservatively widened (e.g. ±30-35%).
    """
    # Case 1: User explicitly provided or confirmed the mass
    if user_confirmed_grams is not None:
        mass = max(0.0, float(user_confirmed_grams))
        return MassBounds(
            grams_min=round(mass, 2),
            grams_center=round(mass, 2),
            grams_max=round(mass, 2),
            is_approximate_plausible_range=False,
            is_user_confirmed=True,
        )

    anchor_spec = get_anchor_spec(visual_anchor)

    # Case 2: Visual volume is estimated from 3D/bounding geometry
    if visual_volume_cm3 is not None and visual_volume_cm3 > 0.0:
        density = get_food_density(food_name, explicit_category=explicit_category)
        # Compute raw mass bounds from volume and density intervals
        m_min_raw = visual_volume_cm3 * density.density_min
        m_center_raw = visual_volume_cm3 * density.density_center
        m_max_raw = visual_volume_cm3 * density.density_max

        # Apply anchor calibration uncertainty
        g_min = max(1.0, m_min_raw * anchor_spec.uncertainty_multiplier_min)
        g_center = max(g_min, m_center_raw)
        g_max = max(g_center, m_max_raw * anchor_spec.uncertainty_multiplier_max)

        return MassBounds(
            grams_min=round(g_min, 1),
            grams_center=round(g_center, 1),
            grams_max=round(g_max, 1),
            is_approximate_plausible_range=True,
            is_user_confirmed=False,
        )

    # Case 3: Prior-based estimation from component identity and count
    prior = get_portion_prior(food_name, explicit_category=explicit_category)
    count = max(0.1, float(detected_count))

    base_min = prior.grams_min * count
    base_center = prior.grams_center * count
    base_max = prior.grams_max * count

    # When no anchor is present, widen uncertainty bounds conservatively
    if visual_anchor == VisualAnchorType.NONE:
        g_min = max(1.0, base_min * anchor_spec.uncertainty_multiplier_min)
        g_center = base_center
        g_max = max(g_center, base_max * anchor_spec.uncertainty_multiplier_max)
    else:
        # With recognized anchor, confidence is higher
        g_min = max(1.0, base_min * anchor_spec.uncertainty_multiplier_min)
        g_center = base_center
        g_max = max(g_center, base_max * anchor_spec.uncertainty_multiplier_max)

    return MassBounds(
        grams_min=round(g_min, 1),
        grams_center=round(g_center, 1),
        grams_max=round(g_max, 1),
        is_approximate_plausible_range=True,
        is_user_confirmed=False,
    )


def convert_volume_to_grams(
    volume_ml: float,
    food_name: str,
    explicit_category: str | None = None,
) -> float:
    """
    Converts volume in milliliters (cm³) to grams using the calibrated food density table.
    """
    density = get_food_density(food_name, explicit_category=explicit_category)
    return round(float(volume_ml) * density.density_center, 1)
