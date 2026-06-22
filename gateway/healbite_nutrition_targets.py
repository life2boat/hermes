from __future__ import annotations

from dataclasses import dataclass

NUTRITION_CALCULATION_VERSION = "mifflin_v1"

SEX_MALE = "male"
SEX_FEMALE = "female"

ACTIVITY_SEDENTARY = "sedentary"
ACTIVITY_LIGHT = "light"
ACTIVITY_MODERATE = "moderate"
ACTIVITY_HIGH = "high"
ACTIVITY_VERY_HIGH = "very_high"

GOAL_LOSE = "lose"
GOAL_MAINTAIN = "maintain"
GOAL_GAIN = "gain"

SUPPORTED_SEXES = {SEX_MALE, SEX_FEMALE}
SUPPORTED_ACTIVITY_LEVELS = {
    ACTIVITY_SEDENTARY,
    ACTIVITY_LIGHT,
    ACTIVITY_MODERATE,
    ACTIVITY_HIGH,
    ACTIVITY_VERY_HIGH,
}
SUPPORTED_GOALS = {
    GOAL_LOSE,
    GOAL_MAINTAIN,
    GOAL_GAIN,
}

SEX_LABELS_RU = {
    SEX_MALE: "Мужской",
    SEX_FEMALE: "Женский",
}

ACTIVITY_LABELS_RU = {
    ACTIVITY_SEDENTARY: "Минимальная активность",
    ACTIVITY_LIGHT: "Лёгкая активность",
    ACTIVITY_MODERATE: "Умеренная активность",
    ACTIVITY_HIGH: "Высокая активность",
    ACTIVITY_VERY_HIGH: "Очень высокая активность",
}

GOAL_LABELS_RU = {
    GOAL_LOSE: "Снижение веса",
    GOAL_MAINTAIN: "Поддержание веса",
    GOAL_GAIN: "Набор массы",
}

ACTIVITY_FACTORS = {
    ACTIVITY_SEDENTARY: 1.20,
    ACTIVITY_LIGHT: 1.375,
    ACTIVITY_MODERATE: 1.55,
    ACTIVITY_HIGH: 1.725,
    ACTIVITY_VERY_HIGH: 1.90,
}

# Product-level MVP configuration. These coefficients should be reviewed by a
# nutrition specialist before any medical positioning of the product.
GOAL_FACTORS = {
    GOAL_LOSE: 0.85,
    GOAL_MAINTAIN: 1.0,
    GOAL_GAIN: 1.10,
}

PROTEIN_GRAMS_PER_KG = 1.6
FAT_GRAMS_PER_KG = 0.8
CALORIES_PER_GRAM_PROTEIN = 4
CALORIES_PER_GRAM_CARBS = 4
CALORIES_PER_GRAM_FAT = 9

MIN_AGE = 18
MAX_AGE = 100
MIN_HEIGHT_CM = 120
MAX_HEIGHT_CM = 230
MIN_WEIGHT_KG = 35
MAX_WEIGHT_KG = 300

CALORIE_DELTA_TOLERANCE = 15


class NutritionTargetValidationError(ValueError):
    pass


@dataclass(slots=True)
class NutritionProfileInputs:
    sex: str
    age: int
    height_cm: float
    weight_kg: float
    activity_level: str
    goal: str


@dataclass(slots=True)
class NutritionTargetCalculation:
    calculation_version: str
    target_source: str
    bmr_kcal: int
    tdee_kcal: int
    daily_kcal_target: int
    daily_protein_g: int
    daily_fat_g: int
    daily_carbs_g: int
    calorie_delta_kcal: int


def normalize_sex(value: str | None) -> str | None:
    token = _normalize_token(value)
    mapping = {
        "male": SEX_MALE,
        "m": SEX_MALE,
        "man": SEX_MALE,
        "м": SEX_MALE,
        "муж": SEX_MALE,
        "мужской": SEX_MALE,
        "female": SEX_FEMALE,
        "f": SEX_FEMALE,
        "woman": SEX_FEMALE,
        "ж": SEX_FEMALE,
        "жен": SEX_FEMALE,
        "женский": SEX_FEMALE,
    }
    return mapping.get(token)


def normalize_goal(value: str | None) -> str | None:
    token = _normalize_token(value)
    mapping = {
        GOAL_LOSE: GOAL_LOSE,
        "снижение веса": GOAL_LOSE,
        "снизить вес": GOAL_LOSE,
        "похудеть": GOAL_LOSE,
        GOAL_MAINTAIN: GOAL_MAINTAIN,
        "поддержание веса": GOAL_MAINTAIN,
        "поддерживать вес": GOAL_MAINTAIN,
        "поддержание": GOAL_MAINTAIN,
        GOAL_GAIN: GOAL_GAIN,
        "набор массы": GOAL_GAIN,
        "набрать массу": GOAL_GAIN,
        "набор": GOAL_GAIN,
    }
    return mapping.get(token)


def normalize_activity_level(value: str | None) -> str | None:
    token = _normalize_token(value)
    mapping = {
        ACTIVITY_SEDENTARY: ACTIVITY_SEDENTARY,
        "минимальная активность": ACTIVITY_SEDENTARY,
        "минимальная": ACTIVITY_SEDENTARY,
        ACTIVITY_LIGHT: ACTIVITY_LIGHT,
        "лёгкая активность": ACTIVITY_LIGHT,
        "легкая активность": ACTIVITY_LIGHT,
        "лёгкая": ACTIVITY_LIGHT,
        "легкая": ACTIVITY_LIGHT,
        ACTIVITY_MODERATE: ACTIVITY_MODERATE,
        "умеренная активность": ACTIVITY_MODERATE,
        "умеренная": ACTIVITY_MODERATE,
        ACTIVITY_HIGH: ACTIVITY_HIGH,
        "высокая активность": ACTIVITY_HIGH,
        "высокая": ACTIVITY_HIGH,
        ACTIVITY_VERY_HIGH: ACTIVITY_VERY_HIGH,
        "очень высокая активность": ACTIVITY_VERY_HIGH,
        "очень высокая": ACTIVITY_VERY_HIGH,
    }
    return mapping.get(token)


def sex_label(value: str | None) -> str:
    normalized = normalize_sex(value)
    return SEX_LABELS_RU.get(normalized or "", "—")


def goal_label(value: str | None) -> str:
    normalized = normalize_goal(value)
    return GOAL_LABELS_RU.get(normalized or "", "—")


def activity_level_label(value: str | None) -> str:
    normalized = normalize_activity_level(value)
    return ACTIVITY_LABELS_RU.get(normalized or "", "—")


def validate_profile_inputs(inputs: NutritionProfileInputs) -> None:
    if inputs.sex not in SUPPORTED_SEXES:
        raise NutritionTargetValidationError("sex")
    if inputs.activity_level not in SUPPORTED_ACTIVITY_LEVELS:
        raise NutritionTargetValidationError("activity_level")
    if inputs.goal not in SUPPORTED_GOALS:
        raise NutritionTargetValidationError("goal")
    if not (MIN_AGE <= int(inputs.age) <= MAX_AGE):
        raise NutritionTargetValidationError("age")
    if not (MIN_HEIGHT_CM <= float(inputs.height_cm) <= MAX_HEIGHT_CM):
        raise NutritionTargetValidationError("height_cm")
    if not (MIN_WEIGHT_KG <= float(inputs.weight_kg) <= MAX_WEIGHT_KG):
        raise NutritionTargetValidationError("weight_kg")


def calculate_nutrition_targets(
    inputs: NutritionProfileInputs,
    *,
    manual_kcal_target: float | None = None,
    target_source: str | None = None,
) -> NutritionTargetCalculation:
    validate_profile_inputs(inputs)

    bmr_raw = _calculate_bmr(inputs)
    tdee_raw = bmr_raw * ACTIVITY_FACTORS[inputs.activity_level]
    calculated_target_raw = tdee_raw * GOAL_FACTORS[inputs.goal]

    effective_target_source = (target_source or ("manual" if manual_kcal_target is not None else "calculated")).strip().lower()
    if effective_target_source not in {"manual", "calculated"}:
        raise NutritionTargetValidationError("target_source")

    effective_target_raw = float(manual_kcal_target) if effective_target_source == "manual" else calculated_target_raw
    if effective_target_raw <= 0:
        raise NutritionTargetValidationError("daily_kcal_target")

    protein_raw = float(inputs.weight_kg) * PROTEIN_GRAMS_PER_KG
    fat_raw = float(inputs.weight_kg) * FAT_GRAMS_PER_KG
    protein_kcal = protein_raw * CALORIES_PER_GRAM_PROTEIN
    fat_kcal = fat_raw * CALORIES_PER_GRAM_FAT
    carbs_kcal_raw = effective_target_raw - protein_kcal - fat_kcal
    if carbs_kcal_raw < 0:
        raise NutritionTargetValidationError("daily_carbs_g")

    carbs_raw = carbs_kcal_raw / CALORIES_PER_GRAM_CARBS

    bmr_kcal = int(round(bmr_raw))
    tdee_kcal = int(round(tdee_raw))
    daily_kcal_target = int(round(effective_target_raw))
    daily_protein_g = int(round(protein_raw))
    daily_fat_g = int(round(fat_raw))
    daily_carbs_g = int(round(carbs_raw))

    rounded_energy = (
        daily_protein_g * CALORIES_PER_GRAM_PROTEIN
        + daily_fat_g * CALORIES_PER_GRAM_FAT
        + daily_carbs_g * CALORIES_PER_GRAM_CARBS
    )
    calorie_delta = abs(int(daily_kcal_target - rounded_energy))
    if calorie_delta > CALORIE_DELTA_TOLERANCE:
        raise NutritionTargetValidationError("rounded_energy_delta")

    return NutritionTargetCalculation(
        calculation_version=NUTRITION_CALCULATION_VERSION,
        target_source=effective_target_source,
        bmr_kcal=bmr_kcal,
        tdee_kcal=tdee_kcal,
        daily_kcal_target=daily_kcal_target,
        daily_protein_g=daily_protein_g,
        daily_fat_g=daily_fat_g,
        daily_carbs_g=daily_carbs_g,
        calorie_delta_kcal=calorie_delta,
    )


def _calculate_bmr(inputs: NutritionProfileInputs) -> float:
    base = 10.0 * float(inputs.weight_kg) + 6.25 * float(inputs.height_cm) - 5.0 * int(inputs.age)
    if inputs.sex == SEX_MALE:
        return base + 5.0
    return base - 161.0


def _normalize_token(value: str | None) -> str:
    return " ".join(str(value or "").strip().casefold().split())
