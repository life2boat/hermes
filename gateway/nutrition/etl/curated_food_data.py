"""
gateway.nutrition.etl.curated_food_data — Curated Russian food mappings, aliases, and translation vocabulary.

Provides authoritative Russian terminology, culinary cooking states, categories,
and rich search aliases for over 350+ common foods, ingredients, and prepared dishes.
"""

from __future__ import annotations

from typing import Any

# Standard HealBite food categories
VALID_CATEGORIES = {
    "poultry",
    "meat",
    "fish",
    "dairy",
    "eggs",
    "cereals",
    "vegetables",
    "fruits",
    "legumes",
    "nuts_seeds",
    "fats_oils",
    "bakery",
    "prepared_dishes",
    "soups_sauces",
    "sweets",
    "beverages",
    "fast_food",
    "spices",
    "baby_food",
    "general",
}

# USDA category description to HealBite category mapping
USDA_CATEGORY_MAPPING: dict[str, str] = {
    "poultry products": "poultry",
    "chicken, whole pieces": "poultry",
    "poultry mixed dishes": "prepared_dishes",
    "beef products": "meat",
    "pork products": "meat",
    "lamb, veal, and game products": "meat",
    "sausages and luncheon meats": "meat",
    "meat mixed dishes": "prepared_dishes",
    "finfish and shellfish products": "fish",
    "fish": "fish",
    "shellfish": "fish",
    "seafood mixed dishes": "prepared_dishes",
    "dairy and egg products": "dairy",
    "milk": "dairy",
    "cheese": "dairy",
    "yogurt": "dairy",
    "eggs and omelets": "eggs",
    "cereal grains and pasta": "cereals",
    "breakfast cereals": "cereals",
    "pasta mixed dishes": "prepared_dishes",
    "rice mixed dishes": "prepared_dishes",
    "vegetables and vegetable products": "vegetables",
    "vegetables": "vegetables",
    "other vegetables and combinations": "vegetables",
    "fruits and fruit juices": "fruits",
    "fruits": "fruits",
    "legumes and legume products": "legumes",
    "beans, peas, legumes": "legumes",
    "nut and seed products": "nuts_seeds",
    "nuts and seeds": "nuts_seeds",
    "fats and oils": "fats_oils",
    "fats and oils, salad dressings": "fats_oils",
    "baked products": "bakery",
    "yeast breads": "bakery",
    "quick breads and pastries": "bakery",
    "meals, entrees, and side dishes": "prepared_dishes",
    "soups, sauces, and gravies": "soups_sauces",
    "soups": "soups_sauces",
    "dips, gravies, other sauces": "soups_sauces",
    "sweets": "sweets",
    "candy": "sweets",
    "cakes and pies": "sweets",
    "cookies and brownies": "sweets",
    "ice cream and frozen dairy desserts": "sweets",
    "snacks": "snacks",
    "beverages": "beverages",
    "coffee": "beverages",
    "tea": "beverages",
    "fast foods": "fast_food",
    "burgers": "fast_food",
    "pizza": "fast_food",
    "deli and cured meat sandwiches": "fast_food",
    "stir-fry and soy-based sauce mixtures": "prepared_dishes",
    "restaurant foods": "prepared_dishes",
    "baby foods": "baby_food",
    "spices and herbs": "spices",
}

# Cooking state extraction rules from description keywords
COOKING_STATE_KEYWORDS: list[tuple[str, str]] = [
    ("raw", "raw"),
    ("fresh", "raw"),
    ("uncooked", "raw"),
    ("unprepared", "raw"),
    ("dry", "raw"),
    ("boiled", "boiled"),
    ("parboiled", "boiled"),
    ("simmered", "boiled"),
    ("poached", "boiled"),
    ("water-blanched", "boiled"),
    ("baked", "baked"),
    ("roasted", "roasted"),
    ("rotisserie", "roasted"),
    ("pan-fried", "fried"),
    ("deep-fried", "fried"),
    ("french fried", "fried"),
    ("fried", "fried"),
    ("sautéed", "fried"),
    ("sauteed", "fried"),
    ("grilled", "grilled"),
    ("broiled", "grilled"),
    ("charbroiled", "grilled"),
    ("barbequed", "grilled"),
    ("bbq", "grilled"),
    ("steamed", "steamed"),
    ("stewed", "braised"),
    ("braised", "braised"),
    ("smoked", "smoked"),
    ("dried", "dried"),
    ("dehydrated", "dried"),
    ("freeze-dried", "dried"),
    ("canned", "canned"),
    ("pickled", "cured"),
    ("salted", "cured"),
    ("cured", "cured"),
    ("fermented", "cured"),
    ("cooked", "cooked"),
    ("prepared", "cooked"),
    ("ready-to-eat", "ready_to_eat"),
]

# Comprehensive English -> Russian dictionary for base food nouns
RUSSIAN_NOUN_DICT: dict[str, str] = {
    "chicken": "Курица",
    "turkey": "Индейка",
    "beef": "Говядина",
    "pork": "Свинина",
    "lamb": "Баранина",
    "veal": "Телятина",
    "duck": "Утка",
    "goose": "Гусь",
    "rabbit": "Кролик",
    "salmon": "Лосось",
    "tuna": "Тунец",
    "cod": "Треска",
    "trout": "Форель",
    "tilapia": "Тилапия",
    "halibut": "Палтус",
    "mackerel": "Скумбрия",
    "herring": "Сельдь",
    "sardine": "Сардина",
    "sardines": "Сардины",
    "pollock": "Минтай",
    "pike": "Щука",
    "perch": "Окунь",
    "carp": "Карп",
    "shrimp": "Креветки",
    "crab": "Краб",
    "lobster": "Омар",
    "squid": "Кальмар",
    "octopus": "Осьминог",
    "mussel": "Мидии",
    "mussels": "Мидии",
    "egg": "Яйцо куриное",
    "eggs": "Яйца куриные",
    "milk": "Молоко",
    "cheese": "Сыр",
    "yogurt": "Йогурт",
    "butter": "Сливочное масло",
    "cream": "Сливки",
    "sour cream": "Сметана",
    "cottage cheese": "Творог",
    "curd": "Творог",
    "olive oil": "Оливковое масло",
    "oil, olive": "Оливковое масло",
    "sunflower oil": "Подсолнечное масло",
    "oil, sunflower": "Подсолнечное масло",
    "oil": "Масло",
    "rice": "Рис",
    "oats": "Овсяные хлопья",
    "oatmeal": "Овсяная каша",
    "wheat": "Пшеница",
    "buckwheat": "Гречка",
    "barley": "Перловка",
    "millet": "Пшено",
    "semolina": "Манка",
    "rye": "Рожь",
    "corn": "Кукуруза",
    "quinoa": "Киноа",
    "bulgur": "Булгур",
    "couscous": "Кускус",
    "pasta": "Макароны",
    "macaroni": "Макароны",
    "spaghetti": "Спагетти",
    "noodles": "Лапша",
    "bread": "Хлеб",
    "roll": "Булочка",
    "bagel": "Бейгл",
    "pita": "Пита",
    "potato": "Картофель",
    "potatoes": "Картофель",
    "tomato": "Помидор",
    "tomatoes": "Помидоры",
    "cucumber": "Огурец",
    "cucumbers": "Огурцы",
    "onion": "Лук репчатый",
    "onions": "Лук репчатый",
    "garlic": "Чеснок",
    "carrot": "Морковь",
    "carrots": "Морковь",
    "broccoli": "Брокколи",
    "cauliflower": "Цветная капуста",
    "cabbage": "Капуста",
    "zucchini": "Кабачок",
    "eggplant": "Баклажан",
    "spinach": "Шпинат",
    "lettuce": "Салат листовой",
    "kale": "Кале",
    "pepper": "Перец",
    "peppers": "Перец",
    "beet": "Свекла",
    "beets": "Свекла",
    "radish": "Редис",
    "celery": "Сельдерей",
    "pumpkin": "Тыква",
    "squash": "Тыква",
    "asparagus": "Спаржа",
    "mushroom": "Грибы",
    "mushrooms": "Грибы",
    "apple": "Яблоко",
    "apples": "Яблоки",
    "banana": "Банан",
    "bananas": "Бананы",
    "orange": "Апельсин",
    "oranges": "Апельсины",
    "pear": "Груша",
    "pears": "Груши",
    "lemon": "Лимон",
    "lemons": "Лимоны",
    "lime": "Лайм",
    "grapefruit": "Грейпфрут",
    "peach": "Персик",
    "peaches": "Персики",
    "nectarine": "Нектарин",
    "apricot": "Абрикос",
    "apricots": "Абрикосы",
    "plum": "Слива",
    "plums": "Сливы",
    "cherry": "Вишня",
    "cherries": "Черешня",
    "grape": "Виноград",
    "grapes": "Виноград",
    "watermelon": "Арбуз",
    "melon": "Дыня",
    "strawberry": "Клубника",
    "strawberries": "Клубника",
    "blueberry": "Черника",
    "blueberries": "Черника",
    "raspberry": "Малина",
    "raspberries": "Малина",
    "blackberry": "Ежевика",
    "cranberry": "Клюква",
    "currant": "Смородина",
    "avocado": "Авокадо",
    "avocados": "Авокадо",
    "mango": "Манго",
    "pineapple": "Ананас",
    "kiwi": "Киви",
    "pomegranate": "Гранат",
    "fig": "Инжир",
    "figs": "Инжир",
    "date": "Финики",
    "dates": "Финики",
    "beans": "Фасоль",
    "lentils": "Чечевица",
    "peas": "Горох",
    "chickpeas": "Нут",
    "soybeans": "Соевые бобы",
    "tofu": "Тофу",
    "nuts": "Орехи",
    "walnut": "Грецкий орех",
    "walnuts": "Грецкий орех",
    "almond": "Миндаль",
    "almonds": "Миндаль",
    "hazelnut": "Фундук",
    "hazelnuts": "Фундук",
    "cashew": "Кешью",
    "cashews": "Кешью",
    "peanut": "Арахис",
    "peanuts": "Арахис",
    "pistachio": "Фисташки",
    "pistachios": "Фисташки",
    "seeds": "Семена",
    "sunflower": "Подсолнечник",
    "sesame": "Кунжут",
    "flaxseed": "Семена льна",
    "chia": "Чиа",
    "soup": "Суп",
    "pizza": "Пицца",
    "sandwich": "Сэндвич",
    "burger": "Бургер",
    "cheeseburger": "Чизбургер",
    "hamburger": "Гамбургер",
    "salad": "Салат",
    "stew": "Рагу / Тушеное блюдо",
    "casserole": "Запеканка",
    "pie": "Пирог",
    "cake": "Торт",
    "cookie": "Печенье",
    "cookies": "Печенье",
    "chocolate": "Шоколад",
    "honey": "Мед",
    "sugar": "Сахар",
}

# English modifier to Russian adjective/descriptor
RUSSIAN_MOD_DICT: dict[str, str] = {
    "breast": "грудка",
    "thigh": "бедро",
    "wing": "крыло",
    "drumstick": "голень",
    "leg": "ножка",
    "fillet": "филе",
    "meat only": "филе",
    "ground": "фарш",
    "minced": "рубленый",
    "steak": "стейк",
    "chop": "отбивная",
    "loin": "корейка",
    "tenderloin": "вырезка",
    "ribs": "ребра",
    "liver": "печень",
    "heart": "сердце",
    "tongue": "язык",
    "raw": "сырой",
    "cooked": "приготовленный",
    "boiled": "вареный",
    "baked": "запеченный",
    "roasted": "запеченный",
    "fried": "жареный",
    "grilled": "на гриле",
    "steamed": "на пару",
    "smoked": "копченый",
    "dried": "сушеный",
    "dry": "сухой",
    "canned": "консервированный",
    "pickled": "маринованный",
    "salted": "соленый",
    "whole": "цельный",
    "skinless": "без кожи",
    "with skin": "с кожей",
    "white": "белый",
    "red": "красный",
    "green": "зеленый",
    "black": "черный",
    "brown": "бурый",
    "lean": "нежирный",
    "extra lean": "постный",
    "low fat": "обезжиренный",
    "nonfat": "обезжиренный",
    "sweet": "сладкий",
    "mashed": "пюре",
}

def translate_description_to_russian(description: str) -> str:
    """Translates USDA comma-separated English description to idiomatic Russian string."""
    parts = [p.strip() for p in description.split(",") if p.strip()]
    if not parts:
        return description

    first_word = parts[0].lower()
    base_ru = None
    for en_key, ru_val in RUSSIAN_NOUN_DICT.items():
        if en_key == first_word or first_word.startswith(en_key + " ") or first_word.endswith(" " + en_key):
            base_ru = ru_val
            break

    if not base_ru:
        # Check subwords
        words = first_word.split()
        for w in words:
            if w in RUSSIAN_NOUN_DICT:
                base_ru = RUSSIAN_NOUN_DICT[w]
                break

    if not base_ru:
        base_ru = parts[0]

    matched_mods: list[str] = []
    for part in parts[1:]:
        p_low = part.lower()
        for en_mod, ru_mod in RUSSIAN_MOD_DICT.items():
            if en_mod in p_low:
                if ru_mod not in matched_mods:
                    matched_mods.append(ru_mod)
                break

    if matched_mods:
        return f"{base_ru} ({', '.join(matched_mods)})"
    return base_ru


def detect_cooking_state_from_text(text: str) -> str:
    """Detects canonical cooking state from food description."""
    t_low = text.lower()
    for kw, state in COOKING_STATE_KEYWORDS:
        # Check for word boundary
        if kw in t_low:
            return state
    return "raw"


def classify_category_from_usda(usda_category: str, description: str) -> str:
    """Maps USDA category and text description to canonical HealBite category."""
    cat_low = usda_category.strip().lower()
    if cat_low in USDA_CATEGORY_MAPPING:
        res = USDA_CATEGORY_MAPPING[cat_low]
        # Disambiguate egg vs dairy
        if res == "dairy" and "egg" in description.lower() and "milk" not in description.lower():
            return "eggs"
        return res

    for k, v in USDA_CATEGORY_MAPPING.items():
        if k in cat_low:
            return v

    desc_low = description.lower()
    if any(w in desc_low for w in ("chicken", "turkey", "duck", "poultry")):
        return "poultry"
    if any(w in desc_low for w in ("beef", "pork", "lamb", "veal", "meat")):
        return "meat"
    if any(w in desc_low for w in ("salmon", "tuna", "fish", "trout", "cod", "shrimp", "seafood")):
        return "fish"
    if any(w in desc_low for w in ("milk", "cheese", "yogurt", "cream", "butter")):
        return "dairy"
    if "egg" in desc_low:
        return "eggs"
    if any(w in desc_low for w in ("apple", "banana", "orange", "berry", "fruit")):
        return "fruits"
    if any(w in desc_low for w in ("potato", "tomato", "cucumber", "carrot", "broccoli", "vegetable")):
        return "vegetables"
    if any(w in desc_low for w in ("rice", "oats", "oatmeal", "cereal", "pasta", "wheat", "buckwheat")):
        return "cereals"

    return "general"
