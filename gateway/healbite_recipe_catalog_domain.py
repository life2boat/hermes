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


class CommercialReuseStatus(str, Enum):
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
    COMMERCIAL_ALLOWED = "COMMERCIAL_ALLOWED"
    PERMITTED = "PERMITTED"
    LICENSE_REQUIRED = "LICENSE_REQUIRED"
    NON_COMMERCIAL_ONLY = "NON_COMMERCIAL_ONLY"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    PROHIBITED = "PROHIBITED"
    UNKNOWN = "UNKNOWN"


class TranslationRightsStatus(str, Enum):
    ORIGINAL_LANGUAGE = "ORIGINAL_LANGUAGE"
    PUBLIC_DOMAIN = "PUBLIC_DOMAIN"
    LICENSED = "LICENSED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    COPYRIGHT_PROTECTED = "COPYRIGHT_PROTECTED"
    UNKNOWN = "UNKNOWN"


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    REJECTED = "REJECTED"


class ContentScope(str, Enum):
    METADATA_ONLY = "METADATA_ONLY"
    STRUCTURED_RECIPE_CONTENT = "STRUCTURED_RECIPE_CONTENT"


class RightsEvidenceType(str, Enum):
    PUBLIC_DOMAIN_STATUTE = "PUBLIC_DOMAIN_STATUTE"
    PUBLISHER_LICENSE = "PUBLISHER_LICENSE"
    OPERATOR_USER_GRANT = "OPERATOR_USER_GRANT"
    LINK_ONLY_CITATION = "LINK_ONLY_CITATION"
    NONE = "NONE"


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
    "oz": "oz",
    "lb": "lb",
    "pt": "pt",
    "qt": "qt",
    "gill": "gill",
    "drop": "drop",
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
    "tablespoon": "tbsp",
    "tablespoons": "tbsp",
    "tablespoonful": "tbsp",
    "tablespoonfuls": "tbsp",
    "ч.л.": "tsp",
    "ч. ложка": "tsp",
    "чайная ложка": "tsp",
    "tsp": "tsp",
    "teaspoon": "tsp",
    "teaspoons": "tsp",
    "teaspoonful": "tsp",
    "teaspoonfuls": "tsp",
    "щепотка": "pinch",
    "pinch": "pinch",
    "pinches": "pinch",
    "oz": "oz",
    "oz.": "oz",
    "ounce": "oz",
    "ounces": "oz",
    "lb": "lb",
    "lb.": "lb",
    "pound": "lb",
    "pounds": "lb",
    "pt": "pt",
    "pt.": "pt",
    "pint": "pt",
    "pints": "pt",
    "qt": "qt",
    "qt.": "qt",
    "quart": "qt",
    "quarts": "qt",
    "gill": "gill",
    "gills": "gill",
    "drop": "drop",
    "drops": "drop",
    # French units
    "gramme": "g",
    "grammes": "g",
    "kilogramme": "kg",
    "kilogrammes": "kg",
    "décilitre": "ml",
    "décilitres": "ml",
    "decilitre": "ml",
    "decilitres": "ml",
    "dl": "ml",
    "centilitre": "ml",
    "centilitres": "ml",
    "cl": "ml",
    "litre": "l",
    "litres": "l",
    "cuillerée": "tbsp",
    "cuillerées": "tbsp",
    "cuilleree": "tbsp",
    "cuillerees": "tbsp",
    "cuillère": "tbsp",
    "cuillères": "tbsp",
    "cuillere": "tbsp",
    "cuilleres": "tbsp",
    "cuillerée à soupe": "tbsp",
    "cuillerée à café": "tsp",
    "pincée": "pinch",
    "pincées": "pinch",
    "pincee": "pinch",
    "pincees": "pinch",
    "pièce": "piece",
    "pièces": "piece",
    "piece": "piece",
    "goutte": "drop",
    "gouttes": "drop",
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
    # English culinary terms for Escoffier catalog
    "egg": "EGG",
    "eggs": "EGG",
    "butter": "BUTTER",
    "brown butter": "BUTTER",
    "onion": "ONION",
    "onions": "ONION",
    "carrot": "CARROT",
    "carrots": "CARROT",
    "chicken": "CHICKEN",
    "chicken fillet": "CHICKEN_BREAST",
    "chicken fillets": "CHICKEN_BREAST",
    "chicken suprême": "CHICKEN_BREAST",
    "chicken suprêmes": "CHICKEN_BREAST",
    "chicken liver": "CHICKEN_LIVER",
    "beef": "BEEF",
    "beef tournedos": "BEEF",
    "tournedos": "BEEF",
    "sole": "FISH_SOLE",
    "cod": "COD",
    "fish": "FISH",
    "truffle": "TRUFFLE",
    "truffles": "TRUFFLE",
    "mushroom": "MUSHROOM",
    "mushrooms": "MUSHROOM",
    "cheese": "CHEESE",
    "grated cheese": "CHEESE",
    "spinach": "SPINACH",
    "spinach-leaves": "SPINACH",
    "flour": "FLOUR",
    "salt": "SALT",
    "pepper": "BLACK_PEPPER",
    "black pepper": "BLACK_PEPPER",
    "milk": "MILK",
    "cream": "CREAM",
    "rice": "RICE",
    "tapioca": "TAPIOCA",
    "potato": "POTATO",
    "potatoes": "POTATO",
    "celery": "CELERY",
    "leek": "LEEK",
    "leeks": "LEEK",
    "parsley": "PARSLEY",
    "sorrel": "SORREL",
    "turnip": "TURNIP",
    "turnips": "TURNIP",
    "bacon": "BACON",
    "ham": "HAM",
    "sausage": "SAUSAGE",
    "vinegar": "VINEGAR",
    "lemon": "LEMON",
    "vegetable oil": "VEGETABLE_OIL",
    "olive oil": "OLIVE_OIL",
    "oil": "VEGETABLE_OIL",
    "shallot": "SHALLOT",
    "shallots": "SHALLOT",
    "white wine": "WHITE_WINE",
    "wine": "WHITE_WINE",
    "consommé": "CONSOMME",
    "consomme": "CONSOMME",
    "chicken consommé": "CHICKEN_CONSOMME",
    "chicken consomme": "CHICKEN_CONSOMME",
    "foie gras": "FOIE_GRAS",
    "foie-gras": "FOIE_GRAS",
    "demi-glace": "DEMI_GLACE",
    "mornay sauce": "SAUCE_MORNAY",
    "chasseur sauce": "SAUCE_CHASSEUR",
    "béarnaise sauce": "SAUCE_BEARNAISE",
    "bearnaise sauce": "SAUCE_BEARNAISE",
    "madeira": "WINE_MADEIRA",
    "madeira sauce": "SAUCE_MADEIRA",
    "tomato": "TOMATO",
    "tomatoes": "TOMATO",
    "tomato purée": "TOMATO",
    "tomato puree": "TOMATO",
    "french beans": "GREEN_BEANS",
    "asparagus": "ASPARAGUS",
    "peas": "PEAS",
    "split peas": "PEAS",
    "beetroot": "BEETROOT",
    "cabbage": "CABBAGE",
    "toast": "BREAD",
    "bread": "BREAD",
    "crusts": "BREAD",
    "breadcrumbs": "BREADCRUMBS",
    "veal": "VEAL",
    "veal cutlet": "VEAL",
    "veal cutlets": "VEAL",
    "lamb": "LAMB",
    "lamb cutlet": "LAMB",
    "lamb cutlets": "LAMB",
    "thyme": "THYME",
    "garlic": "GARLIC",
    "chive": "CHIVES",
    "chives": "CHIVES",
    "tarragon": "TARRAGON",
    "chervil": "CHERVIL",
    "cucumber": "CUCUMBER",
    "cucumbers": "CUCUMBER",
    "lentils": "LENTILS",
    "duck": "DUCK",
    "watercress": "WATERCRESS",
    # French culinary terms for Escoffier original catalog
    "oeuf": "EGG",
    "oeufs": "EGG",
    "œuf": "EGG",
    "œufs": "EGG",
    "beurre": "BUTTER",
    "beurre fondu": "BUTTER",
    "beurre noir": "BUTTER",
    "sel": "SALT",
    "poivre": "BLACK_PEPPER",
    "poivre noir": "BLACK_PEPPER",
    "poivre du moulin": "BLACK_PEPPER",
    "épinards": "SPINACH",
    "epinards": "SPINACH",
    "sauce mornay": "SAUCE_MORNAY",
    "sauce chasseur": "SAUCE_CHASSEUR",
    "sauce demi-glace": "SAUCE_DEMI_GLACE",
    "demi-glace": "SAUCE_DEMI_GLACE",
    "sauce vin blanc": "SAUCE_WHITE_WINE",
    "sauce tomate": "TOMATO",
    "jus de veau": "BROTH_BEEF",
    "jus lié": "BROTH_BEEF",
    "rognon": "MUTTON_KIDNEY",
    "rognon de mouton": "MUTTON_KIDNEY",
    "rognons": "MUTTON_KIDNEY",
    "foie gras": "FOIE_GRAS",
    "truffe": "TRUFFLE",
    "truffes": "TRUFFLE",
    "champignon": "MUSHROOM",
    "champignons": "MUSHROOM",
    "fromage": "CHEESE",
    "fromage râpé": "CHEESE",
    "parmesan": "CHEESE",
    "gruyère": "CHEESE",
    "persil": "PARSLEY",
    "cerfeuil": "CHERVIL",
    "estragon": "TARRAGON",
    "ciboulette": "CHIVES",
    "fines herbes": "HERBS",
    "betterave": "BEETROOT",
    "betteraves": "BEETROOT",
    "carotte": "CARROT",
    "carottes": "CARROT",
    "navet": "TURNIP",
    "navets": "TURNIP",
    "poireau": "LEEK",
    "poireaux": "LEEK",
    "céleri": "CELERY",
    "celeri": "CELERY",
    "pomme de terre": "POTATO",
    "pommes de terre": "POTATO",
    "lentilles": "LENTILS",
    "pois": "PEAS",
    "petits pois": "PEAS",
    "pois cassés": "PEAS",
    "tomate": "TOMATO",
    "tomates": "TOMATO",
    "purée de tomates": "TOMATO",
    "sole": "FISH_SOLE",
    "soles": "FISH_SOLE",
    "filets de sole": "FISH_SOLE",
    "cabillaud": "COD",
    "tournedos": "BEEF",
    "bœuf": "BEEF",
    "boeuf": "BEEF",
    "veau": "VEAL",
    "mouton": "LAMB",
    "agneau": "LAMB",
    "côtelettes": "LAMB",
    "côtelettes de mouton": "LAMB",
    "poulet": "CHICKEN",
    "volaille": "CHICKEN",
    "suprêmes de volaille": "CHICKEN_BREAST",
    "suprême de volaille": "CHICKEN_BREAST",
    "blanc de volaille": "CHICKEN_BREAST",
    "foies de volaille": "CHICKEN_LIVER",
    "foie de volaille": "CHICKEN_LIVER",
    "croûtons": "BREAD",
    "pain": "BREAD",
    "pain de mie": "BREAD",
    "croûtes": "BREAD",
    "consommé": "BROTH_BEEF",
    "consommé blanc": "BROTH_BEEF",
    "bouillon": "BROTH_BEEF",
    "vin blanc": "WHITE_WINE",
    "farine": "FLOUR",
    "crème": "CREAM",
    "creme": "CREAM",
    "lait": "MILK",
    "huile": "VEGETABLE_OIL",
    "tapioca": "TAPIOCA",
    "choucroute": "CABBAGE",
    "chou": "CABBAGE",
    "jambon": "HAM",
    "saucisse": "SAUSAGE",
    "saucisses": "SAUSAGE",
    "chipolatas": "SAUSAGE",
    "vinaigre": "VINEGAR",
    "concombre": "CUCUMBER",
    "concombres": "CUCUMBER",
    "ail": "GARLIC",
    "échalote": "SHALLOT",
    "échalotes": "SHALLOT",
    "echalote": "SHALLOT",
    "echalotes": "SHALLOT",
    "oignon": "ONION",
    "oignons": "ONION",
    "citron": "LEMON",
    "jus de citron": "LEMON",
    "oseille": "SORREL",
    "feuilles d'oseille": "SORREL",
    "consomme": "BROTH_BEEF",
    "consomme blanc": "BROTH_BEEF",
    "bouillon blanc": "BROTH_BEEF",
    "moelle": "BONE_MARROW",
    "olives": "OLIVES",
    "haricots": "BEANS",
    "riz": "RICE",
    "langue": "TONGUE",
    "homard": "LOBSTER",
    "caviar": "CAVIAR",
    "sauce béarnaise": "SAUCE_BEARNAISE",
    "sauce bearnaise": "SAUCE_BEARNAISE",
    "glace de viande": "MEAT_GLAZE",
    "eau": "WATER",
    "poisson": "FISH",
}


def normalize_ingredient_id(display_name: str) -> str:
    cleaned = normalize_title(display_name)
    if cleaned in INGREDIENT_ALIAS_MAP:
        return INGREDIENT_ALIAS_MAP[cleaned]
    # Check direct match with normalized alias keys
    for alias, canonical in INGREDIENT_ALIAS_MAP.items():
        if normalize_title(alias) == cleaned:
            return canonical
    # Check partial match on multi-word
    for alias, canonical in sorted(INGREDIENT_ALIAS_MAP.items(), key=lambda x: len(x[0]), reverse=True):
        norm_alias = normalize_title(alias)
        if norm_alias in cleaned or alias in cleaned:
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
    edition: str | None = None
    language: str = "ru"
    rights_status: RightsStatus = RightsStatus.UNKNOWN
    rights_evidence_type: RightsEvidenceType = RightsEvidenceType.NONE
    rights_evidence_locator: str | None = None
    rights_evidence_note: str | None = None
    source_content_hash: str = ""
    ingestion_timestamp: str = ""
    ingestion_tool_version: str = "1.0.0"
    content_scope: ContentScope = ContentScope.METADATA_ONLY
    verification_status: VerificationStatus = VerificationStatus.VERIFIED
    created_at: str = ""
    underlying_work_rights: str = "PUBLIC_DOMAIN"
    digital_reproduction_rights: str = "PUBLIC_DOMAIN_MARKED"
    digital_reproduction_reuse_terms: str = ""
    commercial_reuse_status: CommercialReuseStatus = CommercialReuseStatus.UNKNOWN
    transcription_source: str = "OWN_EXTRACTION"
    transcription_rights: str = "NOT_APPLICABLE"
    partner_institution_terms: str = ""
    target_jurisdiction_status: str = ""
    translation_status: TranslationRightsStatus = TranslationRightsStatus.ORIGINAL_LANGUAGE
    jurisdiction_basis: str | None = None
    edition_basis: str | None = None
    canonical_url: str | None = None
    production_rights_approved: bool = False


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
    source_recipe_title: str | None = None
    normalized_content_hash: str | None = None
    ingestion_build_id: str | None = None
    production_eligible: bool = True
    source_verification_status: VerificationStatus | None = None
    source_content_scope: ContentScope | None = None
    source_production_rights_approved: bool = False
    classification_source: str | None = None
    classification_reason: str | None = None

    @property
    def is_verified_for_planning(self) -> bool:
        return (
            self.verified
            and self.verification_status is VerificationStatus.VERIFIED
            and self.rights_status not in (RightsStatus.UNKNOWN, RightsStatus.LINK_ONLY)
            and bool(self.source_content_hash)
        )

    @property
    def is_production_cleared(self) -> bool:
        if not (self.is_verified_for_planning and self.production_eligible):
            return False
        return (
            self.source_verification_status is VerificationStatus.VERIFIED
            and self.source_content_scope is ContentScope.STRUCTURED_RECIPE_CONTENT
            and self.source_production_rights_approved is True
        )


@dataclass(frozen=True, slots=True)
class CatalogMetadata:
    schema_version: int
    build_id: str
    content_hash: str
    recipe_count: int
    verified_recipe_count: int
    created_at: str
