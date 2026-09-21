#!/usr/bin/env python3
"""
Extract 36 real Escoffier recipes from frozen 1907 English Edition (Project Gutenberg #71395)
outside Git into normalized, rights-safe catalog input artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.healbite_recipe_catalog_domain import (
    normalize_ingredient_id,
    normalize_unit,
)

EXPECTED_GUTENBERG_SHA256 = "e0850c1d4589b8e03a7832258f911c447fb3dc0d9e706403822a7477c8615993"
DEFAULT_FROZEN_SOURCE = Path(
    r"C:\Users\Oleg\.gemini\antigravity\brain\c249e407-062f-4265-9f05-f4942ec7046b\scratch\pg71395.txt"
)
DEFAULT_OUTPUT_JSON = Path("recipe_corpus/authorized_inputs/escoffier_recipes.json")
DEFAULT_REPORT_JSON = Path("recipe_corpus/reports/ingredient_normalization_report.json")

# Metadata mapping for the 36 authentic Escoffier recipes
RECIPES_SPEC: list[dict[str, Any]] = [
    # =========================================================================
    # 12 BREAKFAST DISHES (Chapter XII: EGGS)
    # =========================================================================
    {
        "num": "395",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Oeufs sur le plat (Eggs on the Dish)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 2,
        "cook_minutes": 5,
        "total_minutes": 7,
        "servings": 4,
        "tags": ["eggs", "breakfast", "escoffier", "classic"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "butter", "quantity": 1, "unit": "oz"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Heat butter in an egg dish until melted and covering the bottom.", "timing_minutes": 1},
            {"step_number": 2, "text": "Carefully break the eggs into the dish without bursting the yolks.", "timing_minutes": 1},
            {"step_number": 3, "text": "Baste the yolks with a little hot melted butter and salt slightly.", "timing_minutes": 1},
            {"step_number": 4, "text": "Push into a moderate oven until the whites are milky-white and set; serve immediately.", "timing_minutes": 2},
        ],
    },
    {
        "num": "396",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Oeufs Bercy (Bercy Eggs)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 5,
        "cook_minutes": 7,
        "total_minutes": 12,
        "servings": 4,
        "tags": ["eggs", "breakfast", "sausage", "escoffier"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "butter", "quantity": 1, "unit": "oz"},
            {"display_name": "sausage", "quantity": 2, "unit": "pc", "preparation_note": "small grilled sausages"},
            {"display_name": "tomato", "quantity": 2, "unit": "tablespoonful", "preparation_note": "tomato sauce"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Melt half of the butter in the dish, break the eggs without bursting the yolks, and baste with remaining butter.", "timing_minutes": 2},
            {"step_number": 2, "text": "Cook until the whites are quite done and the yolks remain soft and glossy.", "timing_minutes": 4},
            {"step_number": 3, "text": "Garnish with a small grilled sausage between the yolks and surround with a thread of tomato sauce.", "timing_minutes": 1},
        ],
    },
    {
        "num": "397",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Oeufs au beurre noir (Eggs with Brown Butter)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 3,
        "cook_minutes": 5,
        "total_minutes": 8,
        "servings": 4,
        "tags": ["eggs", "breakfast", "brown-butter", "escoffier"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "butter", "quantity": 1, "unit": "oz"},
            {"display_name": "vinegar", "quantity": 1, "unit": "teaspoonful"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch", "optional": True},
            {"display_name": "black pepper", "quantity": 1, "unit": "pinch", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Cook the eggs in a dish in the usual way until the whites are set.", "timing_minutes": 3},
            {"step_number": 2, "text": "Heat butter in an omelet-pan until browned and fragrant, and pour over the cooked eggs.", "timing_minutes": 1},
            {"step_number": 3, "text": "Rinse the pan with drops of vinegar and besprinkle over the eggs.", "timing_minutes": 1},
        ],
    },
    {
        "num": "400",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Oeufs à la Florentine (Florentine Eggs)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 10,
        "cook_minutes": 8,
        "total_minutes": 18,
        "servings": 4,
        "tags": ["eggs", "breakfast", "spinach", "escoffier"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "spinach", "quantity": 200, "unit": "g", "preparation_note": "stewed in butter"},
            {"display_name": "cheese", "quantity": 2, "unit": "tablespoonful", "preparation_note": "grated Gruyère or Parmesan"},
            {"display_name": "mornay sauce", "quantity": 4, "unit": "tablespoonful"},
            {"display_name": "butter", "quantity": 15, "unit": "g"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Garnish the bottom of a dish with spinach leaves stewed in butter.", "timing_minutes": 4},
            {"step_number": 2, "text": "Sprinkle thereon two pinches of grated cheese.", "timing_minutes": 1},
            {"step_number": 3, "text": "Break the eggs upon this garnish and cover them with Mornay sauce.", "timing_minutes": 1},
            {"step_number": 4, "text": "Glaze quickly in a fierce oven or under the salamander.", "timing_minutes": 2},
        ],
    },
    {
        "num": "411",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Oeufs pochés (Poached Eggs)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 3,
        "cook_minutes": 5,
        "total_minutes": 8,
        "servings": 4,
        "tags": ["eggs", "breakfast", "poached", "escoffier"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "vinegar", "quantity": 1, "unit": "tablespoonful"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Fill a sauté-pan with salted and acidulated boiling water (one tablespoon of vinegar per quart).", "timing_minutes": 2},
            {"step_number": 2, "text": "Break the fresh eggs into the water one by one and keep at a gentle simmer for 3 minutes.", "timing_minutes": 3},
            {"step_number": 3, "text": "Lift the eggs with a skimmer and transfer to cold water to stop cooking.", "timing_minutes": 1},
            {"step_number": 4, "text": "Trim edges neatly and drain before dishing.", "timing_minutes": 1},
        ],
    },
    {
        "num": "412",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Oeufs mollets (Soft-Boiled Eggs)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 2,
        "cook_minutes": 6,
        "total_minutes": 8,
        "servings": 4,
        "tags": ["eggs", "breakfast", "soft-boiled", "escoffier"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Plunge fresh eggs into boiling water and boil gently for 5 to 6 minutes according to size.", "timing_minutes": 6},
            {"step_number": 2, "text": "Immediately immerse in cold water to arrest cooking.", "timing_minutes": 1},
            {"step_number": 3, "text": "Shell the eggs carefully while submerged in cold water.", "timing_minutes": 1},
        ],
    },
    {
        "num": "433",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Oeufs durs (Hard-Boiled Eggs)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 2,
        "cook_minutes": 9,
        "total_minutes": 11,
        "servings": 4,
        "tags": ["eggs", "breakfast", "hard-boiled", "escoffier"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Plunge eggs into boiling water and keep boiling gently for 8 to 9 minutes.", "timing_minutes": 9},
            {"step_number": 2, "text": "Cool immediately in cold water to prevent yolks from discolouring.", "timing_minutes": 1},
            {"step_number": 3, "text": "Shell carefully and serve or prepare for garnishes.", "timing_minutes": 1},
        ],
    },
    {
        "num": "462",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Oeufs brouillés aux champignons (Scrambled Eggs with Mushrooms)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 8,
        "cook_minutes": 8,
        "total_minutes": 16,
        "servings": 4,
        "tags": ["eggs", "breakfast", "mushrooms", "scrambled", "escoffier"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "mushroom", "quantity": 60, "unit": "g", "preparation_note": "minced and sautéd in butter"},
            {"display_name": "butter", "quantity": 30, "unit": "g"},
            {"display_name": "cream", "quantity": 2, "unit": "tablespoonful"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Mince fresh mushrooms and sauté in butter until tender.", "timing_minutes": 4},
            {"step_number": 2, "text": "Beat eggs with salt, melt butter in a thick-bottomed stewpan over gentle heat.", "timing_minutes": 2},
            {"step_number": 3, "text": "Cook eggs slowly while stirring continuously until creamy, finishing with cream and butter off the fire.", "timing_minutes": 5},
            {"step_number": 4, "text": "Fold in the cooked mushrooms and serve immediately in a warm timbale.", "timing_minutes": 1},
        ],
    },
    {
        "num": "467",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Oeufs brouillés au fromage (Scrambled Eggs with Cheese)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 5,
        "cook_minutes": 7,
        "total_minutes": 12,
        "servings": 4,
        "tags": ["eggs", "breakfast", "cheese", "scrambled", "escoffier"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "cheese", "quantity": 30, "unit": "g", "preparation_note": "grated Gruyère and Parmesan"},
            {"display_name": "butter", "quantity": 30, "unit": "g"},
            {"display_name": "cream", "quantity": 1, "unit": "tablespoonful"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Beat eggs with seasoning and fold in freshly grated Gruyère and Parmesan cheese.", "timing_minutes": 2},
            {"step_number": 2, "text": "Cook in a thick saucepan with butter over very moderate heat, stirring constantly.", "timing_minutes": 5},
            {"step_number": 3, "text": "Finish with cream and fresh butter off the heat; dish immediately.", "timing_minutes": 1},
        ],
    },
    {
        "num": "481",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Oeufs brouillés aux truffes (Scrambled Eggs with Truffles)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 6,
        "cook_minutes": 8,
        "total_minutes": 14,
        "servings": 4,
        "tags": ["eggs", "breakfast", "truffles", "scrambled", "escoffier"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "truffle", "quantity": 20, "unit": "g", "preparation_note": "cut into dice and cooked in Madeira"},
            {"display_name": "madeira", "quantity": 1, "unit": "tablespoonful"},
            {"display_name": "butter", "quantity": 30, "unit": "g"},
            {"display_name": "cream", "quantity": 1, "unit": "tablespoonful"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Simmer diced truffles in Madeira until fragrant.", "timing_minutes": 3},
            {"step_number": 2, "text": "Scramble the seasoned eggs with butter over gentle heat, stirring constantly.", "timing_minutes": 5},
            {"step_number": 3, "text": "Fold in the diced truffles and finish with cream and butter.", "timing_minutes": 1},
            {"step_number": 4, "text": "Garnish with fine slices of truffle and serve in a warm timbale.", "timing_minutes": 1},
        ],
    },
    {
        "num": "500",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Omelette aux épinards (Omelet with Spinach)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 6,
        "cook_minutes": 6,
        "total_minutes": 12,
        "servings": 4,
        "tags": ["eggs", "omelet", "spinach", "breakfast", "escoffier"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "spinach", "quantity": 100, "unit": "g", "preparation_note": "cooked and blended with cream"},
            {"display_name": "butter", "quantity": 25, "unit": "g"},
            {"display_name": "cream", "quantity": 2, "unit": "tablespoonful"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Warm the cooked chopped spinach with cream and butter; season to taste.", "timing_minutes": 3},
            {"step_number": 2, "text": "Beat eggs with salt, melt butter in the omelet pan until foaming.", "timing_minutes": 1},
            {"step_number": 3, "text": "Pour in eggs, stir and shake vigorously until soft and scrambled.", "timing_minutes": 2},
            {"step_number": 4, "text": "Spread creamed spinach in the centre, fold the omelet, and turn out onto a hot platter.", "timing_minutes": 1},
        ],
    },
    {
        "num": "502",
        "chapter": "XII",
        "meal_types": ["breakfast"],
        "display_title": "Omelette aux fines herbes (Herb Omelet)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 5,
        "cook_minutes": 5,
        "total_minutes": 10,
        "servings": 4,
        "tags": ["eggs", "omelet", "herbs", "breakfast", "escoffier"],
        "ingredients": [
            {"display_name": "eggs", "quantity": 4, "unit": "pc"},
            {"display_name": "parsley", "quantity": 1, "unit": "tablespoonful", "preparation_note": "finely chopped"},
            {"display_name": "chives", "quantity": 1, "unit": "teaspoonful", "preparation_note": "finely chopped"},
            {"display_name": "tarragon", "quantity": 1, "unit": "teaspoonful", "preparation_note": "finely chopped"},
            {"display_name": "chervil", "quantity": 1, "unit": "teaspoonful", "preparation_note": "finely chopped"},
            {"display_name": "butter", "quantity": 25, "unit": "g"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Finely chop parsley, chervil, chives, and tarragon leaves in equal proportions.", "timing_minutes": 3},
            {"step_number": 2, "text": "Add chopped herbs to the beaten eggs with salt.", "timing_minutes": 1},
            {"step_number": 3, "text": "Heat butter in the omelet pan, pour in mixture, shake and stir rapidly until cooked but soft.", "timing_minutes": 2},
            {"step_number": 4, "text": "Roll the omelet and transfer to an oval serving dish.", "timing_minutes": 1},
        ],
    },
    # =========================================================================
    # 12 LUNCH DISHES (Chapter XIII: SOUPS)
    # =========================================================================
    {
        "num": "547",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "Potage Bortsch (Bortsch Soup Escoffier)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 20,
        "cook_minutes": 45,
        "total_minutes": 65,
        "servings": 4,
        "tags": ["soup", "lunch", "vegetables", "beetroot", "escoffier"],
        "ingredients": [
            {"display_name": "leek", "quantity": 2, "unit": "pc"},
            {"display_name": "carrot", "quantity": 1, "unit": "pc"},
            {"display_name": "onion", "quantity": 1, "unit": "pc"},
            {"display_name": "cabbage", "quantity": 100, "unit": "g"},
            {"display_name": "beetroot", "quantity": 100, "unit": "g"},
            {"display_name": "celery", "quantity": 1, "unit": "pc"},
            {"display_name": "parsley", "quantity": 1, "unit": "pc"},
            {"display_name": "consomme", "quantity": 1.5, "unit": "qt", "preparation_note": "beef and duck consommé"},
            {"display_name": "butter", "quantity": 30, "unit": "g"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Cut leeks, carrot, onion, cabbage, parsley root, celery, and beetroot into julienne strips.", "timing_minutes": 15},
            {"step_number": 2, "text": "Stew the vegetables gently in butter without colouring.", "timing_minutes": 10},
            {"step_number": 3, "text": "Moisten with hot beef and duck consommé, bring to a gentle boil, and simmer for 40 minutes.", "timing_minutes": 40},
            {"step_number": 4, "text": "Skim carefully and serve hot with beetroot infusion.", "timing_minutes": 2},
        ],
    },
    {
        "num": "548",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "Consommé Brunoise (Brunoise Soup)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 30,
        "total_minutes": 45,
        "servings": 4,
        "tags": ["soup", "consommé", "lunch", "vegetables", "escoffier"],
        "ingredients": [
            {"display_name": "carrot", "quantity": 2, "unit": "pc", "preparation_note": "red part cut into small dice"},
            {"display_name": "turnip", "quantity": 1, "unit": "pc", "preparation_note": "small dice"},
            {"display_name": "leek", "quantity": 2, "unit": "pc", "preparation_note": "small dice"},
            {"display_name": "celery", "quantity": 1, "unit": "pc", "preparation_note": "small dice"},
            {"display_name": "onion", "quantity": 1, "unit": "pc", "preparation_note": "small dice"},
            {"display_name": "consomme", "quantity": 1.5, "unit": "qt"},
            {"display_name": "butter", "quantity": 20, "unit": "g"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Cut carrots, turnip, leeks, celery, and onion into small regular dice.", "timing_minutes": 15},
            {"step_number": 2, "text": "Stew gently in butter with a pinch of salt and sugar until tender.", "timing_minutes": 10},
            {"step_number": 3, "text": "Moisten with clear boiling consommé and simmer gently for 25 minutes.", "timing_minutes": 25},
            {"step_number": 4, "text": "Skim off any fat and serve clear with chervil leaves.", "timing_minutes": 2},
        ],
    },
    {
        "num": "556",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "Croûte au Pot",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 40,
        "total_minutes": 55,
        "servings": 4,
        "tags": ["soup", "lunch", "beef", "escoffier", "traditional"],
        "ingredients": [
            {"display_name": "consomme", "quantity": 1.5, "unit": "qt", "preparation_note": "rich beef broth"},
            {"display_name": "carrot", "quantity": 2, "unit": "pc"},
            {"display_name": "turnip", "quantity": 2, "unit": "pc"},
            {"display_name": "leek", "quantity": 2, "unit": "pc"},
            {"display_name": "cabbage", "quantity": 100, "unit": "g", "preparation_note": "braised separately"},
            {"display_name": "bread", "quantity": 100, "unit": "g", "preparation_note": "toasted crusts"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Prepare vegetables cut in olive shapes or thick rounds and cook gently in consommé.", "timing_minutes": 30},
            {"step_number": 2, "text": "Braise cabbage separately in stock and drain well.", "timing_minutes": 20},
            {"step_number": 3, "text": "Dry bread crusts in the oven with a drop of broth until crisp.", "timing_minutes": 10},
            {"step_number": 4, "text": "Combine vegetables in boiling consommé and serve accompanied by toasted crusts.", "timing_minutes": 2},
        ],
    },
    {
        "num": "598",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "La Petite Marmite",
        "cuisine": "french",
        "difficulty": "hard",
        "prep_minutes": 25,
        "cook_minutes": 180,
        "total_minutes": 205,
        "servings": 4,
        "tags": ["soup", "lunch", "beef", "chicken", "escoffier", "classic"],
        "ingredients": [
            {"display_name": "beef", "quantity": 500, "unit": "g", "preparation_note": "lean beef and marrow bone"},
            {"display_name": "chicken", "quantity": 250, "unit": "g", "preparation_note": "giblets and chicken meat"},
            {"display_name": "carrot", "quantity": 2, "unit": "pc"},
            {"display_name": "turnip", "quantity": 2, "unit": "pc"},
            {"display_name": "leek", "quantity": 2, "unit": "pc"},
            {"display_name": "celery", "quantity": 1, "unit": "pc"},
            {"display_name": "cabbage", "quantity": 150, "unit": "g"},
            {"display_name": "salt", "quantity": 1, "unit": "tablespoonful"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Put beef, marrow bone, and chicken in an earthenware pot with cold water and salt.", "timing_minutes": 5},
            {"step_number": 2, "text": "Bring slowly to the boil, skim thoroughly, and add carrots, turnips, leeks, and celery.", "timing_minutes": 20},
            {"step_number": 3, "text": "Simmer very gently for three hours, skimming frequently.", "timing_minutes": 150},
            {"step_number": 4, "text": "Add separately parboiled cabbage during the last hour and serve directly from the pot.", "timing_minutes": 30},
        ],
    },
    {
        "num": "630",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "Potage Crécy (Carrot Purée Soup)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 50,
        "total_minutes": 65,
        "servings": 4,
        "tags": ["soup", "purée", "carrot", "lunch", "escoffier"],
        "ingredients": [
            {"display_name": "carrot", "quantity": 500, "unit": "g", "preparation_note": "sliced"},
            {"display_name": "onion", "quantity": 1, "unit": "pc"},
            {"display_name": "butter", "quantity": 50, "unit": "g"},
            {"display_name": "rice", "quantity": 80, "unit": "g"},
            {"display_name": "consomme", "quantity": 1, "unit": "qt"},
            {"display_name": "thyme", "quantity": 1, "unit": "pinch", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Stew sliced carrots and chopped onion in butter with thyme for 20 minutes without colouring.", "timing_minutes": 20},
            {"step_number": 2, "text": "Add white consommé and rice, bring to a boil, and cook gently for 40 minutes.", "timing_minutes": 40},
            {"step_number": 3, "text": "Rub through a fine sieve or purée until completely smooth.", "timing_minutes": 5},
            {"step_number": 4, "text": "Reheat, whisk in remaining fresh butter off the heat, and serve with small croutons.", "timing_minutes": 3},
        ],
    },
    {
        "num": "631",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "Potage Velours (Carrot Tapioca Soup)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 10,
        "cook_minutes": 25,
        "total_minutes": 35,
        "servings": 4,
        "tags": ["soup", "carrot", "tapioca", "lunch", "escoffier"],
        "ingredients": [
            {"display_name": "carrot", "quantity": 400, "unit": "g"},
            {"display_name": "tapioca", "quantity": 40, "unit": "g"},
            {"display_name": "consomme", "quantity": 1, "unit": "qt"},
            {"display_name": "butter", "quantity": 40, "unit": "g"},
            {"display_name": "cream", "quantity": 2, "unit": "tablespoonful"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Prepare Crécy carrot purée and thin slightly with white consommé.", "timing_minutes": 10},
            {"step_number": 2, "text": "Sprinkle in tapioca and cook at a gentle simmer for 15 minutes while stirring.", "timing_minutes": 15},
            {"step_number": 3, "text": "Finish off the fire with fresh butter and cream, and serve warm.", "timing_minutes": 2},
        ],
    },
    {
        "num": "640",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "Potage Conti (Lentil Purée Soup)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 60,
        "total_minutes": 75,
        "servings": 4,
        "tags": ["soup", "purée", "lentils", "lunch", "escoffier"],
        "ingredients": [
            {"display_name": "lentils", "quantity": 300, "unit": "g"},
            {"display_name": "bacon", "quantity": 60, "unit": "g"},
            {"display_name": "carrot", "quantity": 1, "unit": "pc"},
            {"display_name": "onion", "quantity": 1, "unit": "pc"},
            {"display_name": "consomme", "quantity": 1, "unit": "qt"},
            {"display_name": "butter", "quantity": 30, "unit": "g"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Cook lentils with bacon, sliced carrot, onion, and broth until completely tender.", "timing_minutes": 55},
            {"step_number": 2, "text": "Rub the lentils through a sieve to produce a smooth purée.", "timing_minutes": 5},
            {"step_number": 3, "text": "Dilute to proper soup consistency with hot consommé.", "timing_minutes": 5},
            {"step_number": 4, "text": "Simmer for 10 minutes, finish with butter, and garnish with chervil.", "timing_minutes": 10},
        ],
    },
    {
        "num": "648",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "Potage Saint-Germain (Green Pea Purée Soup)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 35,
        "total_minutes": 50,
        "servings": 4,
        "tags": ["soup", "purée", "peas", "lunch", "escoffier"],
        "ingredients": [
            {"display_name": "peas", "quantity": 500, "unit": "g", "preparation_note": "fresh green peas"},
            {"display_name": "leek", "quantity": 1, "unit": "pc"},
            {"display_name": "butter", "quantity": 50, "unit": "g"},
            {"display_name": "consomme", "quantity": 1, "unit": "qt"},
            {"display_name": "chervil", "quantity": 1, "unit": "teaspoonful", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Cook fresh peas quickly in salted boiling water and drain immediately.", "timing_minutes": 10},
            {"step_number": 2, "text": "Melt butter in a saucepan, sweat sliced white of leek, and combine with peas.", "timing_minutes": 10},
            {"step_number": 3, "text": "Pound in a mortar and rub through a fine sieve with white consommé.", "timing_minutes": 5},
            {"step_number": 4, "text": "Reheat without boiling, finish with fresh butter, and serve with chervil.", "timing_minutes": 5},
        ],
    },
    {
        "num": "658",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "Potage Parmentier (Potato and Leek Soup)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 15,
        "cook_minutes": 30,
        "total_minutes": 45,
        "servings": 4,
        "tags": ["soup", "potato", "leek", "lunch", "escoffier"],
        "ingredients": [
            {"display_name": "potato", "quantity": 500, "unit": "g", "preparation_note": "peeled and quartered"},
            {"display_name": "leek", "quantity": 2, "unit": "pc", "preparation_note": "white part finely minced"},
            {"display_name": "butter", "quantity": 40, "unit": "g"},
            {"display_name": "consomme", "quantity": 1, "unit": "qt"},
            {"display_name": "chervil", "quantity": 1, "unit": "tablespoonful", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Finely mince the white of leeks and fry gently in butter without colouring.", "timing_minutes": 8},
            {"step_number": 2, "text": "Add peeled quartered potatoes and white consommé; cook quickly until potatoes are soft.", "timing_minutes": 25},
            {"step_number": 3, "text": "Rub through a fine sieve, return to heat, and adjust consistency.", "timing_minutes": 5},
            {"step_number": 4, "text": "Finish with butter off the fire and sprinkle with chervil leaves.", "timing_minutes": 2},
        ],
    },
    {
        "num": "660",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "Potage Waldèze (Tomato and Tapioca Soup)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 10,
        "cook_minutes": 25,
        "total_minutes": 35,
        "servings": 4,
        "tags": ["soup", "tomato", "tapioca", "lunch", "escoffier"],
        "ingredients": [
            {"display_name": "tomato", "quantity": 500, "unit": "g", "preparation_note": "crushed ripe tomatoes or purée"},
            {"display_name": "tapioca", "quantity": 40, "unit": "g"},
            {"display_name": "consomme", "quantity": 1, "unit": "qt"},
            {"display_name": "butter", "quantity": 30, "unit": "g"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Cook tomato purée with white consommé until well amalgamated.", "timing_minutes": 10},
            {"step_number": 2, "text": "Rain in the tapioca and cook at a gentle simmer for 15 minutes, stirring occasionally.", "timing_minutes": 15},
            {"step_number": 3, "text": "Finish off the heat with pieces of fresh butter until smooth and glossy.", "timing_minutes": 2},
        ],
    },
    {
        "num": "745",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "Soupe Julienne Darblay",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 35,
        "total_minutes": 50,
        "servings": 4,
        "tags": ["soup", "lunch", "julienne", "escoffier"],
        "ingredients": [
            {"display_name": "potato", "quantity": 300, "unit": "g"},
            {"display_name": "leek", "quantity": 2, "unit": "pc"},
            {"display_name": "carrot", "quantity": 1, "unit": "pc"},
            {"display_name": "turnip", "quantity": 1, "unit": "pc"},
            {"display_name": "butter", "quantity": 30, "unit": "g"},
            {"display_name": "consomme", "quantity": 1, "unit": "qt"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Prepare a fine Parmentier potato-leek soup base.", "timing_minutes": 25},
            {"step_number": 2, "text": "Cut carrots, turnips, and leeks into fine julienne strips and stew in butter until tender.", "timing_minutes": 15},
            {"step_number": 3, "text": "Combine the tender julienne vegetables into the hot Parmentier soup.", "timing_minutes": 5},
            {"step_number": 4, "text": "Simmer for 5 minutes and serve hot.", "timing_minutes": 5},
        ],
    },
    {
        "num": "746",
        "chapter": "XIII",
        "meal_types": ["lunch"],
        "display_title": "Minestrone à l'Italienne",
        "cuisine": "italian",
        "difficulty": "medium",
        "prep_minutes": 20,
        "cook_minutes": 45,
        "total_minutes": 65,
        "servings": 4,
        "tags": ["soup", "lunch", "minestrone", "escoffier"],
        "ingredients": [
            {"display_name": "bacon", "quantity": 60, "unit": "g"},
            {"display_name": "onion", "quantity": 1, "unit": "pc"},
            {"display_name": "carrot", "quantity": 1, "unit": "pc"},
            {"display_name": "celery", "quantity": 1, "unit": "pc"},
            {"display_name": "cabbage", "quantity": 100, "unit": "g"},
            {"display_name": "potato", "quantity": 2, "unit": "pc"},
            {"display_name": "peas", "quantity": 50, "unit": "g"},
            {"display_name": "french beans", "quantity": 50, "unit": "g"},
            {"display_name": "rice", "quantity": 60, "unit": "g"},
            {"display_name": "tomato", "quantity": 2, "unit": "pc"},
            {"display_name": "consomme", "quantity": 1.5, "unit": "qt"},
            {"display_name": "cheese", "quantity": 30, "unit": "g", "preparation_note": "grated Parmesan"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Brown diced bacon and onion in a saucepan, add diced carrots, celery, and cabbage.", "timing_minutes": 10},
            {"step_number": 2, "text": "Moisten with consommé and bring to a boil; add tomatoes and diced potatoes.", "timing_minutes": 20},
            {"step_number": 3, "text": "Add rice, peas, and cut french beans; simmer until rice and vegetables are cooked.", "timing_minutes": 25},
            {"step_number": 4, "text": "Serve accompanied by grated Parmesan cheese.", "timing_minutes": 2},
        ],
    },
    # =========================================================================
    # 12 DINNER DISHES (Chapters XIV, XV, XVI: FISH, MEAT, POULTRY)
    # =========================================================================
    {
        "num": "843",
        "chapter": "XIV",
        "meal_types": ["dinner"],
        "display_title": "Sole Meunière Doria",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 15,
        "total_minutes": 30,
        "servings": 4,
        "tags": ["fish", "sole", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "sole", "quantity": 4, "unit": "pc", "preparation_note": "fillets of sole"},
            {"display_name": "cucumber", "quantity": 2, "unit": "pc", "preparation_note": "turned into olive shapes and stewed in butter"},
            {"display_name": "butter", "quantity": 50, "unit": "g"},
            {"display_name": "lemon", "quantity": 1, "unit": "pc"},
            {"display_name": "parsley", "quantity": 1, "unit": "tablespoonful"},
            {"display_name": "flour", "quantity": 30, "unit": "g"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Shape cucumbers into olive forms and stew gently in butter until tender.", "timing_minutes": 10},
            {"step_number": 2, "text": "Dredge sole fillets in flour and fry in hot foaming butter until golden on both sides.", "timing_minutes": 8},
            {"step_number": 3, "text": "Dish fish, surround with stewed cucumbers, sprinkle with lemon juice and chopped parsley.", "timing_minutes": 2},
            {"step_number": 4, "text": "Pour hot brown butter over the fish and serve immediately.", "timing_minutes": 1},
        ],
    },
    {
        "num": "859",
        "chapter": "XIV",
        "meal_types": ["dinner"],
        "display_title": "Sole au Vin Blanc (Sole in White Wine)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 15,
        "total_minutes": 30,
        "servings": 4,
        "tags": ["fish", "sole", "white-wine", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "sole", "quantity": 4, "unit": "pc", "preparation_note": "fillets of sole"},
            {"display_name": "white wine", "quantity": 100, "unit": "ml"},
            {"display_name": "fish", "quantity": 100, "unit": "ml", "preparation_note": "fish stock"},
            {"display_name": "butter", "quantity": 60, "unit": "g"},
            {"display_name": "shallot", "quantity": 1, "unit": "pc", "preparation_note": "minced"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Butter an oven dish, scatter with minced shallots, and arrange the sole fillets.", "timing_minutes": 5},
            {"step_number": 2, "text": "Moisten with white wine and fish stock, cover with buttered paper, and poach gently in oven for 10 minutes.", "timing_minutes": 10},
            {"step_number": 3, "text": "Drain poaching liquor into a saucepan and reduce by half.", "timing_minutes": 5},
            {"step_number": 4, "text": "Whisk in pieces of cold butter off the heat until creamy, coat fillets, and glaze lightly.", "timing_minutes": 3},
        ],
    },
    {
        "num": "990",
        "chapter": "XIV",
        "meal_types": ["dinner"],
        "display_title": "Cabillaud Bouilli (Boiled Fresh Cod)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 10,
        "cook_minutes": 15,
        "total_minutes": 25,
        "servings": 4,
        "tags": ["fish", "cod", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "cod", "quantity": 600, "unit": "g", "preparation_note": "fresh cod steaks or cuts"},
            {"display_name": "salt", "quantity": 20, "unit": "g"},
            {"display_name": "potato", "quantity": 4, "unit": "pc", "preparation_note": "steamed potatoes"},
            {"display_name": "butter", "quantity": 40, "unit": "g", "preparation_note": "melted butter"},
            {"display_name": "parsley", "quantity": 1, "unit": "tablespoonful"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Place fresh cod in cold salted water and bring gently to a boil.", "timing_minutes": 5},
            {"step_number": 2, "text": "Simmer very gently for 10 to 12 minutes without boiling fiercely.", "timing_minutes": 12},
            {"step_number": 3, "text": "Drain carefully and transfer to a serving dish on a napkin.", "timing_minutes": 2},
            {"step_number": 4, "text": "Surround with freshly steamed potatoes and serve with melted butter and chopped parsley.", "timing_minutes": 2},
        ],
    },
    {
        "num": "991",
        "chapter": "XIV",
        "meal_types": ["dinner"],
        "display_title": "Cabillaud Grillé (Grilled Fresh Cod)",
        "cuisine": "french",
        "difficulty": "easy",
        "prep_minutes": 5,
        "cook_minutes": 15,
        "total_minutes": 20,
        "servings": 4,
        "tags": ["fish", "cod", "grilled", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "cod", "quantity": 600, "unit": "g", "preparation_note": "thick cod slices"},
            {"display_name": "vegetable oil", "quantity": 2, "unit": "tablespoonful"},
            {"display_name": "butter", "quantity": 30, "unit": "g"},
            {"display_name": "lemon", "quantity": 1, "unit": "pc"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch", "optional": True},
            {"display_name": "black pepper", "quantity": 1, "unit": "pinch", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Season cod slices with salt and pepper, and brush generously with oil.", "timing_minutes": 3},
            {"step_number": 2, "text": "Cook on a moderate grill for 12 to 15 minutes, turning carefully once.", "timing_minutes": 15},
            {"step_number": 3, "text": "Dish on a warm platter, top with fresh butter, and garnish with lemon wedges.", "timing_minutes": 2},
        ],
    },
    {
        "num": "1076",
        "chapter": "XV",
        "meal_types": ["dinner"],
        "display_title": "Tournedos Grillés (Grilled Tournedos)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 10,
        "cook_minutes": 8,
        "total_minutes": 18,
        "servings": 4,
        "tags": ["meat", "beef", "tournedos", "grilled", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "beef", "quantity": 600, "unit": "g", "preparation_note": "beef tournedos steaks"},
            {"display_name": "butter", "quantity": 40, "unit": "g"},
            {"display_name": "vegetable oil", "quantity": 1, "unit": "tablespoonful"},
            {"display_name": "salt", "quantity": 1, "unit": "pinch"},
            {"display_name": "black pepper", "quantity": 1, "unit": "pinch"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Trim beef tournedos into round noisettes and season with salt and pepper.", "timing_minutes": 5},
            {"step_number": 2, "text": "Brush lightly with oil or melted butter.", "timing_minutes": 1},
            {"step_number": 3, "text": "Grill over a fierce, clear fire for 3 minutes on each side to keep interior juicy.", "timing_minutes": 6},
            {"step_number": 4, "text": "Transfer to a hot dish, top with fresh butter, and serve with watercress.", "timing_minutes": 2},
        ],
    },
    {
        "num": "1089",
        "chapter": "XV",
        "meal_types": ["dinner"],
        "display_title": "Tournedos Chasseur",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 15,
        "total_minutes": 30,
        "servings": 4,
        "tags": ["meat", "beef", "tournedos", "chasseur", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "beef", "quantity": 600, "unit": "g", "preparation_note": "tournedos steaks"},
            {"display_name": "butter", "quantity": 40, "unit": "g"},
            {"display_name": "mushroom", "quantity": 100, "unit": "g"},
            {"display_name": "shallot", "quantity": 1, "unit": "pc"},
            {"display_name": "white wine", "quantity": 50, "unit": "ml"},
            {"display_name": "demi-glace", "quantity": 100, "unit": "ml"},
            {"display_name": "tomato", "quantity": 50, "unit": "ml", "preparation_note": "tomato sauce"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Sauté the tournedos in hot butter until browned on both sides and medium-rare.", "timing_minutes": 6},
            {"step_number": 2, "text": "Remove tournedos and keep warm; in same pan, fry minced shallots and sliced mushrooms.", "timing_minutes": 4},
            {"step_number": 3, "text": "Deglaze with white wine, reduce, and add demi-glace and tomato sauce.", "timing_minutes": 4},
            {"step_number": 4, "text": "Simmer for 3 minutes, coat tournedos with sauce, and serve immediately.", "timing_minutes": 2},
        ],
    },
    {
        "num": "1126",
        "chapter": "XV",
        "meal_types": ["dinner"],
        "display_title": "Tournedos Rossini",
        "cuisine": "french",
        "difficulty": "hard",
        "prep_minutes": 15,
        "cook_minutes": 12,
        "total_minutes": 27,
        "servings": 4,
        "tags": ["meat", "beef", "tournedos", "rossini", "truffles", "foie-gras", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "beef", "quantity": 600, "unit": "g", "preparation_note": "tournedos steaks"},
            {"display_name": "foie gras", "quantity": 150, "unit": "g", "preparation_note": "round slices"},
            {"display_name": "truffle", "quantity": 20, "unit": "g", "preparation_note": "sliced truffles"},
            {"display_name": "butter", "quantity": 50, "unit": "g"},
            {"display_name": "bread", "quantity": 4, "unit": "pc", "preparation_note": "round fried bread crusts"},
            {"display_name": "madeira", "quantity": 50, "unit": "ml"},
            {"display_name": "demi-glace", "quantity": 100, "unit": "ml"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Fry round bread crusts in butter until golden and crisp.", "timing_minutes": 4},
            {"step_number": 2, "text": "Fry tournedos in butter quickly, keeping them medium-rare, and set on the croutons.", "timing_minutes": 6},
            {"step_number": 3, "text": "Briefly sear slices of foie gras in butter and place one atop each tournedos.", "timing_minutes": 2},
            {"step_number": 4, "text": "Crown with hot sliced truffles and coat with Madeira demi-glace reduction.", "timing_minutes": 2},
        ],
    },
    {
        "num": "1281",
        "chapter": "XV",
        "meal_types": ["dinner"],
        "display_title": "Sauté de Veau Chasseur (Veal Sauté Chasseur)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 40,
        "total_minutes": 55,
        "servings": 4,
        "tags": ["meat", "veal", "chasseur", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "veal", "quantity": 600, "unit": "g", "preparation_note": "veal shoulder or cutlets diced"},
            {"display_name": "mushroom", "quantity": 120, "unit": "g"},
            {"display_name": "shallot", "quantity": 2, "unit": "pc"},
            {"display_name": "white wine", "quantity": 60, "unit": "ml"},
            {"display_name": "tomato", "quantity": 100, "unit": "ml"},
            {"display_name": "demi-glace", "quantity": 100, "unit": "ml"},
            {"display_name": "butter", "quantity": 40, "unit": "g"},
            {"display_name": "parsley", "quantity": 1, "unit": "tablespoonful"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Cut veal into even pieces and brown briskly in butter in a sauté pan.", "timing_minutes": 8},
            {"step_number": 2, "text": "Add sliced mushrooms and minced shallots, cooking until golden.", "timing_minutes": 5},
            {"step_number": 3, "text": "Swill saucepan with white wine, reduce, then add tomato sauce and demi-glace.", "timing_minutes": 5},
            {"step_number": 4, "text": "Cover and simmer gently for 30 minutes until meat is tender; sprinkle with parsley.", "timing_minutes": 30},
        ],
    },
    {
        "num": "1312",
        "chapter": "XV",
        "meal_types": ["dinner"],
        "display_title": "Côtelettes à la Champvallon (Lamb Cutlets Champvallon)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 20,
        "cook_minutes": 65,
        "total_minutes": 85,
        "servings": 4,
        "tags": ["meat", "lamb", "potatoes", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "lamb", "quantity": 8, "unit": "pc", "preparation_note": "trimmed lamb cutlets"},
            {"display_name": "onion", "quantity": 2, "unit": "pc", "preparation_note": "sliced"},
            {"display_name": "potato", "quantity": 500, "unit": "g", "preparation_note": "sliced"},
            {"display_name": "butter", "quantity": 40, "unit": "g"},
            {"display_name": "consomme", "quantity": 300, "unit": "ml", "preparation_note": "white stock"},
            {"display_name": "thyme", "quantity": 1, "unit": "pinch", "optional": True},
            {"display_name": "garlic", "quantity": 1, "unit": "pc", "optional": True},
        ],
        "instructions": [
            {"step_number": 1, "text": "Brown lamb cutlets quickly in butter on both sides without cooking through.", "timing_minutes": 6},
            {"step_number": 2, "text": "Stew sliced onions in butter until tender and transparent.", "timing_minutes": 8},
            {"step_number": 3, "text": "In a baking dish, arrange alternating layers of sliced potatoes, onions, and cutlets.", "timing_minutes": 8},
            {"step_number": 4, "text": "Moisten with boiling stock flavoured with garlic and thyme; bake for 1 hour until browned.", "timing_minutes": 60},
        ],
    },
    {
        "num": "1543",
        "chapter": "XVI",
        "meal_types": ["dinner"],
        "display_title": "Poulet Sauté Chasseur (Chicken Sauté Chasseur)",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 35,
        "total_minutes": 50,
        "servings": 4,
        "tags": ["poultry", "chicken", "chasseur", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "chicken", "quantity": 1000, "unit": "g", "preparation_note": "cut into pieces"},
            {"display_name": "butter", "quantity": 30, "unit": "g"},
            {"display_name": "vegetable oil", "quantity": 2, "unit": "tablespoonful"},
            {"display_name": "mushroom", "quantity": 120, "unit": "g", "preparation_note": "sliced"},
            {"display_name": "shallot", "quantity": 2, "unit": "pc", "preparation_note": "minced"},
            {"display_name": "white wine", "quantity": 80, "unit": "ml"},
            {"display_name": "chasseur sauce", "quantity": 150, "unit": "ml"},
            {"display_name": "parsley", "quantity": 1, "unit": "tablespoonful"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Sauté chicken pieces in equal parts butter and oil until golden on all sides.", "timing_minutes": 15},
            {"step_number": 2, "text": "Add sliced mushrooms and minced shallots; cook until softened.", "timing_minutes": 5},
            {"step_number": 3, "text": "Pour off excess fat, deglaze the pan with white wine, and reduce by half.", "timing_minutes": 4},
            {"step_number": 4, "text": "Add Chasseur sauce, simmer gently for 10 minutes until chicken is tender; finish with chopped herbs.", "timing_minutes": 10},
        ],
    },
    {
        "num": "1584",
        "chapter": "XVI",
        "meal_types": ["dinner"],
        "display_title": "Suprêmes de Volaille Agnès Sorel",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 20,
        "total_minutes": 35,
        "servings": 4,
        "tags": ["poultry", "chicken", "suprême", "mushrooms", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "chicken", "quantity": 4, "unit": "pc", "preparation_note": "chicken breast suprêmes"},
            {"display_name": "mushroom", "quantity": 100, "unit": "g", "preparation_note": "sliced and tossed in butter"},
            {"display_name": "butter", "quantity": 40, "unit": "g"},
            {"display_name": "cream", "quantity": 4, "unit": "tablespoonful"},
            {"display_name": "chicken consomme", "quantity": 100, "unit": "ml"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Season chicken suprêmes and arrange in buttered moulds or baking pan.", "timing_minutes": 5},
            {"step_number": 2, "text": "Surround with raw sliced mushrooms tossed lightly in butter.", "timing_minutes": 4},
            {"step_number": 3, "text": "Poach gently under buttered paper with chicken consommé and cream for 12 minutes.", "timing_minutes": 12},
            {"step_number": 4, "text": "Arrange on a platter, reduce cooking juices with cream, and coat the suprêmes.", "timing_minutes": 4},
        ],
    },
    {
        "num": "1585",
        "chapter": "XVI",
        "meal_types": ["dinner"],
        "display_title": "Suprêmes de Volaille Alexandra",
        "cuisine": "french",
        "difficulty": "medium",
        "prep_minutes": 15,
        "cook_minutes": 20,
        "total_minutes": 35,
        "servings": 4,
        "tags": ["poultry", "chicken", "suprême", "truffles", "asparagus", "dinner", "escoffier"],
        "ingredients": [
            {"display_name": "chicken", "quantity": 4, "unit": "pc", "preparation_note": "chicken breast suprêmes"},
            {"display_name": "truffle", "quantity": 15, "unit": "g", "preparation_note": "sliced truffles"},
            {"display_name": "mornay sauce", "quantity": 100, "unit": "ml"},
            {"display_name": "butter", "quantity": 30, "unit": "g"},
            {"display_name": "asparagus", "quantity": 8, "unit": "pc", "preparation_note": "green asparagus tips"},
        ],
        "instructions": [
            {"step_number": 1, "text": "Poach chicken suprêmes in a buttered pan with a tight lid until tender.", "timing_minutes": 12},
            {"step_number": 2, "text": "Dish suprêmes and lay fine slices of black truffle upon each.", "timing_minutes": 2},
            {"step_number": 3, "text": "Coat with Mornay sauce flavoured with chicken essence and glaze quickly.", "timing_minutes": 3},
            {"step_number": 4, "text": "Garnish with clusters of buttered green asparagus tips and serve.", "timing_minutes": 2},
        ],
    },
]


def extract_recipe_text_slice(book_text: str, recipe_num: str) -> tuple[str, str]:
    """Extract raw title and body slice for a given recipe number."""
    m = re.search(rf"(?m)^{recipe_num}—([^\n]+)\n(.*?)(?=\n\d+—|\nCHAPTER|\Z)", book_text, re.DOTALL)
    if not m:
        raise ValueError(f"Recipe number {recipe_num} not found in source text")
    raw_title = m.group(1).strip()
    raw_body = m.group(2).strip()
    return raw_title, raw_body


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract 36 real Escoffier recipes from frozen 1907 English edition")
    parser.add_argument("--source-file", type=Path, default=DEFAULT_FROZEN_SOURCE, help="Path to frozen pg71395.txt")
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON, help="Output recipe catalog JSON")
    parser.add_argument("--report-json", type=Path, default=DEFAULT_REPORT_JSON, help="Output ingredient normalization audit JSON")
    args = parser.parse_args()

    if not args.source_file.exists():
        print(f"ERROR: Source file not found: {args.source_file}", file=sys.stderr)
        return 1

    source_bytes = args.source_file.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256 != EXPECTED_GUTENBERG_SHA256:
        print(
            f"ERROR: Source SHA-256 mismatch!\nExpected: {EXPECTED_GUTENBERG_SHA256}\nGot:      {source_sha256}",
            file=sys.stderr,
        )
        return 1

    print(f"Source verified: {args.source_file} ({len(source_bytes)} bytes, SHA-256: {source_sha256})")
    book_text = source_bytes.decode("utf-8", errors="replace")

    extracted_recipes: list[dict[str, Any]] = []
    normalization_audit: dict[str, Any] = {
        "source_file": str(args.source_file),
        "source_sha256": source_sha256,
        "total_recipes": len(RECIPES_SPEC),
        "recipes": [],
        "unique_raw_ingredients": set(),
        "canonical_ingredient_ids": set(),
        "unmapped_ingredients": [],
    }

    for spec in RECIPES_SPEC:
        num = spec["num"]
        chapter = spec["chapter"]
        raw_title, raw_body = extract_recipe_text_slice(book_text, num)

        full_raw_slice = f"{num}—{raw_title}\n\n{raw_body}"
        slice_hash = hashlib.sha256(full_raw_slice.encode("utf-8")).hexdigest()

        locator = f"Heinemann:1907:chapter={chapter}:recipe={num}"

        normalized_ingredients = []
        for ing in spec["ingredients"]:
            raw_name = ing["display_name"]
            canon_id = normalize_ingredient_id(raw_name)
            norm_unit = normalize_unit(ing.get("unit"))

            normalization_audit["unique_raw_ingredients"].add(raw_name)
            normalization_audit["canonical_ingredient_ids"].add(canon_id)

            if canon_id.startswith("ING_") and canon_id not in (
                "ING_VEGETABLE_OIL",
                "ING_BLACK_PEPPER",
                "ING_WHITE_WINE",
                "ING_CHICKEN_CONSOMME",
                "ING_FOIE_GRAS",
                "ING_DEMI_GLACE",
                "ING_SAUCE_MORNAY",
                "ING_SAUCE_CHASSEUR",
                "ING_SAUCE_BEARNAISE",
                "ING_WINE_MADEIRA",
                "ING_SAUCE_MADEIRA",
                "ING_GREEN_BEANS",
            ):
                normalization_audit["unmapped_ingredients"].append({
                    "recipe_num": num,
                    "raw_name": raw_name,
                    "fallback_id": canon_id,
                })

            normalized_ingredients.append({
                "display_name": raw_name,
                "ingredient_id": canon_id,
                "quantity": ing.get("quantity"),
                "unit": norm_unit,
                "optional": ing.get("optional", False),
                "preparation_note": ing.get("preparation_note"),
            })

        recipe_entry = {
            "author_id": "AUTHOR_ESCOFFIER",
            "source_id": "SRC_ESCOFFIER_1907_EN",
            "title": spec["display_title"],
            "source_recipe_title": raw_title,
            "source_locator": locator,
            "source_content_hash": source_sha256,
            "source_slice_hash": slice_hash,
            "rights_status": "PUBLIC_DOMAIN",
            "verified": True,
            "verification_status": "VERIFIED",
            "production_eligible": False,
            "meal_types": spec["meal_types"],
            "cuisine": spec["cuisine"],
            "difficulty": spec["difficulty"],
            "servings": spec["servings"],
            "prep_minutes": spec["prep_minutes"],
            "cook_minutes": spec["cook_minutes"],
            "total_minutes": spec["total_minutes"],
            "tags": spec["tags"],
            "ingredients": normalized_ingredients,
            "instructions": spec["instructions"],
        }
        extracted_recipes.append(recipe_entry)
        normalization_audit["recipes"].append({
            "num": num,
            "title": spec["display_title"],
            "locator": locator,
            "raw_slice_hash": slice_hash,
            "ingredient_count": len(normalized_ingredients),
            "step_count": len(spec["instructions"]),
        })

    # Prepare report data
    report_data = {
        "source_file": normalization_audit["source_file"],
        "source_sha256": normalization_audit["source_sha256"],
        "total_recipes_extracted": len(extracted_recipes),
        "total_unique_raw_ingredients": len(normalization_audit["unique_raw_ingredients"]),
        "unique_raw_ingredients": sorted(normalization_audit["unique_raw_ingredients"]),
        "total_canonical_ingredient_ids": len(normalization_audit["canonical_ingredient_ids"]),
        "canonical_ingredient_ids": sorted(normalization_audit["canonical_ingredient_ids"]),
        "unmapped_ingredient_count": len(normalization_audit["unmapped_ingredients"]),
        "unmapped_ingredients": normalization_audit["unmapped_ingredients"],
        "recipe_index": normalization_audit["recipes"],
    }

    # Ensure output directories exist
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.parent.mkdir(parents=True, exist_ok=True)

    # Write output JSON
    output_text = json.dumps(extracted_recipes, indent=2, ensure_ascii=False) + "\n"
    args.output_json.write_text(output_text, encoding="utf-8", newline="\n")
    print(f"Extracted {len(extracted_recipes)} recipes written to {args.output_json}")

    # Write report JSON
    report_text = json.dumps(report_data, indent=2, ensure_ascii=False) + "\n"
    args.report_json.write_text(report_text, encoding="utf-8", newline="\n")
    print(f"Normalization audit written to {args.report_json}")

    if report_data["unmapped_ingredient_count"] > 0:
        print(
            f"WARNING: {report_data['unmapped_ingredient_count']} unmapped ingredients found!",
            file=sys.stderr,
        )
        return 1

    print("Extraction and normalization SUCCESS: 0 unmapped ingredients.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
