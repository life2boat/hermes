from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Sequence

RECIPE_CATALOG_NAMESPACE = uuid.UUID("3d4b6c8e-5b12-4c28-98e1-0c5a3d7e8b9f")

# Initial Author Identifiers
AUTHOR_POKHLEBKIN = "AUTHOR_POKHLEBKIN"
AUTHOR_ESCOFFIER = "AUTHOR_ESCOFFIER"
AUTHOR_JAMIE_OLIVER = "AUTHOR_JAMIE_OLIVER"

INITIAL_AUTHORS = {
    AUTHOR_POKHLEBKIN: "Вильям Васильевич Похлёбкин",
    AUTHOR_ESCOFFIER: "Огюст Эскофье",
    AUTHOR_JAMIE_OLIVER: "Джейми Оливер",
}


class RightsStatus(str, Enum):
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
    LICENSED = "LICENSED"
    USER_PROVIDED_AUTHORIZED = "USER_PROVIDED_AUTHORIZED"
    LINK_ONLY = "LINK_ONLY"
    UNKNOWN = "UNKNOWN"


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class SourceType(str, Enum):
    BOOK = "BOOK"
    WEBSITE = "WEBSITE"
    LICENSED_DATASET = "LICENSED_DATASET"
    USER_DOCUMENT = "USER_DOCUMENT"
    OTHER = "OTHER"


class MealType(str, Enum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


CANONICAL_UNITS = {
    "g": "g",
    "kg": "kg",
    "ml": "ml",
    "l": "l",
    "piece": "piece",
    "package": "package",
    "can": "can",
    "tbsp": "tbsp",
    "tsp": "tsp",
    "pinch": "pinch",
    "unknown": "unknown",
}

UNIT_ALIASES = {
    "г": "g",
    "гр": "g",
    "грамм": "g",
    "граммов": "g",
    "g": "g",
    "кг": "kg",
    "килограмм": "kg",
    "кг.": "kg",
    "kg": "kg",
    "мл": "ml",
    "мл.": "ml",
    "миллилитр": "ml",
    "миллилитров": "ml",
    "ml": "ml",
    "л": "l",
    "л.": "l",
    "литр": "l",
    "литров": "l",
    "l": "l",
    "шт": "piece",
    "шт.": "piece",
    "штук": "piece",
    "штука": "piece",
    "штуки": "piece",
    "piece": "piece",
    "pcs": "piece",
    "уп": "package",
    "упак": "package",
    "упаковка": "package",
    "пачка": "package",
    "package": "package",
    "банка": "can",
    "бан": "can",
    "can": "can",
    "ст.л.": "tbsp",
    "ст. ложка": "tbsp",
    "столовая ложка": "tbsp",
    "tbsp": "tbsp",
    "ч.л.": "tsp",
    "ч. ложка": "tsp",
    "чайная ложка": "tsp",
    "tsp": "tsp",
    "щепотка": "pinch",
    "pinch": "pinch",
}


def normalize_unit(unit_raw: str | None) -> str:
    if not unit_raw:
        return "unknown"
    normalized = unit_raw.strip().lower()
    return UNIT_ALIASES.get(normalized, CANONICAL_UNITS.get(normalized, "unknown"))


def normalize_title(title: str) -> str:
    decomposed = unicodedata.normalize("NFKD", title)
    lowered = decomposed.lower()
    cleaned = re.sub(r"[^\w\s-]", "", lowered)
    return " ".join(cleaned.split())


# Common ingredient normalization aliases for culinary planning
INGREDIENT_ALIAS_MAP: dict[str, str] = {
    "картофель": "POTATO",
    "картошка": "POTATO",
    "картофеля": "POTATO",
    "картофелины": "POTATO",
    "молоко": "MILK",
    "молока": "MILK",
    "лук": "ONION",
    "репчатый лук": "ONION",
    "лук репчатый": "ONION",
    "морковь": "CARROT",
    "морковка": "CARROT",
    "моркови": "CARROT",
    "чеснок": "GARLIC",
    "чеснока": "GARLIC",
    "яйцо": "EGG",
    "яйца": "EGG",
    "яиц": "EGG",
    "куриное яйцо": "EGG",
    "курица": "CHICKEN",
    "куриное филе": "CHICKEN_BREAST",
    "куриная грудка": "CHICKEN_BREAST",
    "филе курицы": "CHICKEN_BREAST",
    "говядина": "BEEF",
    "говядины": "BEEF",
    "филе говядины": "BEEF",
    "свинина": "PORK",
    "свинины": "PORK",
    "индейка": "TURKEY",
    "филе индейки": "TURKEY",
    "рыба": "FISH",
    "лосось": "SALMON",
    "семга": "SALMON",
    "треска": "COD",
    "рис": "RICE",
    "крупа рисовая": "RICE",
    "гречка": "BUCKWHEAT",
    "гречневая крупа": "BUCKWHEAT",
    "овсянка": "OATS",
    "овсяные хлопья": "OATS",
    "макароны": "PASTA",
    "спагетти": "PASTA",
    "паста": "PASTA",
    "сыр": "CHEESE",
    "сыра": "CHEESE",
    "пармезан": "CHEESE_PARMESAN",
    "творог": "COTTAGE_CHEESE",
    "масло сливочное": "BUTTER",
    "сливочное масло": "BUTTER",
    "масло растительное": "VEGETABLE_OIL",
    "растительное масло": "VEGETABLE_OIL",
    "оливковое масло": "OLIVE_OIL",
    "масло оливковое": "OLIVE_OIL",
    "помидоры": "TOMATO",
    "помидор": "TOMATO",
    "томаты": "TOMATO",
    "огурцы": "CUCUMBER",
    "огурец": "CUCUMBER",
    "капуста": "CABBAGE",
    "капуста белокочанная": "CABBAGE",
    "брокколи": "BROCCOLI",
    "кабачок": "ZUCCHINI",
    "кабачки": "ZUCCHINI",
    "яблоко": "APPLE",
    "яблоки": "APPLE",
    "банан": "BANANA",
    "бананы": "BANANA",
    "соль": "SALT",
    "черный перец": "BLACK_PEPPER",
    "перец черный": "BLACK_PEPPER",
    "сахар": "SUGAR",
    "мука": "FLOUR",
    "пшеничная мука": "FLOUR",
    "мука пшеничная": "FLOUR",
}


def normalize_ingredient_id(display_name: str) -> str:
    cleaned = normalize_title(display_name)
    if cleaned in INGREDIENT_ALIAS_MAP:
        return INGREDIENT_ALIAS_MAP[cleaned]
    # Check partial match on multi-word
    for alias, canonical in sorted(INGREDIENT_ALIAS_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        if alias in cleaned:
            return canonical
    # Fallback: uppercase slug
    slug = re.sub(r"[^\w]+", "_", cleaned).strip("_").upper()
    return slug or "INGREDIENT_UNKNOWN"


def generate_recipe_id(
    author_id: str,
    source_id: str,
    source_locator: str | None,
    normalized_title_str: str,
) -> str:
    identity_tuple = (
        author_id.strip(),
        source_id.strip(),
        (source_locator or "").strip(),
        normalized_title_str.strip(),
    )
    identity_string = ":".join(identity_tuple)
    return str(uuid.uuid5(RECIPE_CATALOG_NAMESPACE, identity_string))


@dataclass(frozen=True, slots=True)
class RecipeAuthor:
    author_id: str
    display_name: str
    bio: str | None = None
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class RecipeSource:
    source_id: str
    author_id: str
    title: str
    source_type: SourceType = SourceType.BOOK
    source_locator: str | None = None
    publication_year: int | None = None
    language: str = "ru"
    rights_status: RightsStatus = RightsStatus.UNKNOWN
    source_content_hash: str = ""
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class RecipeIngredient:
    id: str
    recipe_id: str
    ingredient_id: str
    display_name: str
    quantity: Decimal | None
    unit: str = "unknown"
    optional: bool = False
    preparation_note: str | None = None
    position: int = 1


@dataclass(frozen=True, slots=True)
class RecipeInstruction:
    step_number: int
    text: str
    timing_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class RecipeAdaptation:
    adaptation_id: str
    recipe_id: str
    original_ingredient_id: str
    replacement_ingredient_id: str
    ratio: Decimal = Decimal("1.0")
    reason: str = ""
    source_verified: bool = True


@dataclass(frozen=True, slots=True)
class Recipe:
    recipe_id: str
    recipe_version: int
    author_id: str
    source_id: str
    title: str
    normalized_title: str
    meal_types: tuple[MealType, ...]
    cuisine: str | None
    servings: Decimal
    prep_minutes: int | None
    cook_minutes: int | None
    total_minutes: int | None
    difficulty: str | None
    ingredients: tuple[RecipeIngredient, ...]
    instructions: tuple[RecipeInstruction, ...]
    tags: tuple[str, ...]
    source_locator: str | None
    source_content_hash: str
    rights_status: RightsStatus
    verified: bool
    verification_status: VerificationStatus
    created_at: str

    @property
    def is_verified_for_planning(self) -> bool:
        return (
            self.verified
            and self.verification_status is VerificationStatus.VERIFIED
            and self.rights_status not in (RightsStatus.UNKNOWN, RightsStatus.LINK_ONLY)
        )


@dataclass(frozen=True, slots=True)
class CatalogMetadata:
    schema_version: int
    build_id: str
    content_hash: str
    recipe_count: int
    verified_recipe_count: int
    created_at: str
