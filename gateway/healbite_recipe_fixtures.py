from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from gateway.healbite_recipe_catalog_domain import (
    AUTHOR_ESCOFFIER,
    AUTHOR_JAMIE_OLIVER,
    AUTHOR_POKHLEBKIN,
    CommercialReuseStatus,
    ContentScope,
    MealType,
    RecipeAuthor,
    RecipeSource,
    RightsEvidenceType,
    RightsStatus,
    SourceType,
    TranslationRightsStatus,
    VerificationStatus,
)
from gateway.healbite_recipe_catalog_store import HealBiteRecipeCatalogStore
from gateway.healbite_recipe_ingestion import (
    CatalogReadinessReport,
    RecipeIngestionPipeline,
    calculate_catalog_readiness,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Neutral Test Authors for Automated Tests
TEST_AUTHOR_A = "TEST_AUTHOR_A"
TEST_AUTHOR_B = "TEST_AUTHOR_B"
TEST_AUTHOR_C = "TEST_AUTHOR_C"

TEST_AUTHORS: dict[str, str] = {
    TEST_AUTHOR_A: "Шеф-повар Северной кухни (Тест А)",
    TEST_AUTHOR_B: "Классический мастер соусов (Тест Б)",
    TEST_AUTHOR_C: "Домашний кулинар средиземноморья (Тест В)",
}

# Production Manifests (Metadata Only, Rights-Safe, Zero Copyrighted Text)
PRODUCTION_MANIFESTS = [
    {
        "author": RecipeAuthor(
            author_id=AUTHOR_POKHLEBKIN,
            display_name="Вильям Васильевич Похлёбкин",
            bio="Советский и российский историк, крупнейший специалист по кулинарии и истории кулинарии.",
            created_at="2026-09-21 00:00:00",
        ),
        "sources": [
            RecipeSource(
                source_id="SRC_POKHLEBKIN_BEKA",
                author_id=AUTHOR_POKHLEBKIN,
                title="Большая энциклопедия кулинарного искусства",
                source_type=SourceType.BOOK,
                source_locator="Издание Центрполиграф, 2004",
                publication_year=2004,
                edition="1-е изд., Центрполиграф",
                language="ru",
                rights_status=RightsStatus.LINK_ONLY,
                rights_evidence_type=RightsEvidenceType.LINK_ONLY_CITATION,
                rights_evidence_locator="ISBN 5-9524-0718-4",
                rights_evidence_note="Copyright protected under Russian Civil Code Art. 1281 (author died 2000; term expires 2071); LINK_ONLY metadata citation only; no structured recipe content license.",
                source_content_hash="meta_only_pokhlebkin_beka",
                ingestion_timestamp="2026-09-21 00:00:00",
                ingestion_tool_version="1.0.0",
                content_scope=ContentScope.METADATA_ONLY,
                verification_status=VerificationStatus.VERIFIED,
                created_at="2026-09-21 00:00:00",
            ),
        ],
    },
    {
        "author": RecipeAuthor(
            author_id=AUTHOR_ESCOFFIER,
            display_name="Огюст Эскофье",
            bio="Французский ресторатор и кулинарный критик, один из величайших поваров французской кухни.",
            created_at="2026-09-21 00:00:00",
        ),
        "sources": [
            RecipeSource(
                source_id="SRC_ESCOFFIER_LGC",
                author_id=AUTHOR_ESCOFFIER,
                title="Le Guide Culinaire (1903)",
                source_type=SourceType.BOOK,
                source_locator="First edition, Paris, 1903",
                publication_year=1903,
                edition="1re édition, Paris, Aux bureaux de L'Art culinaire",
                language="fr",
                rights_status=RightsStatus.PUBLIC_DOMAIN,
                rights_evidence_type=RightsEvidenceType.PUBLIC_DOMAIN_STATUTE,
                rights_evidence_locator="Bibliothèque nationale de France (BnF) ark:/12148/bpt6k65768837",
                rights_evidence_note=(
                    "Published in France 1903 (author d. 1935); in public domain in US (pre-1929) "
                    "and France (70y pma + wartime extensions elapsed); BnF CGU requires separate commercial licensing; currently METADATA_ONLY."
                ),
                source_content_hash="meta_only_escoffier_1903",
                ingestion_timestamp="2026-09-21 00:00:00",
                ingestion_tool_version="1.0.0",
                content_scope=ContentScope.METADATA_ONLY,
                verification_status=VerificationStatus.VERIFIED,
                created_at="2026-09-21 00:00:00",
                underlying_work_rights="PUBLIC_DOMAIN",
                digital_reproduction_reuse_terms=(
                    "BnF Gallica Conditions Générales d'Utilisation (CGU): Free for non-commercial/academic use; "
                    "commercial reuse requires prior licence agreement and royalty fee."
                ),
                commercial_reuse_status=CommercialReuseStatus.LICENSE_REQUIRED,
                partner_institution_terms="BnF Gallica / Wellcome Library digital scan b21525912",
                target_jurisdiction_status="France CPI Art. L.123-1 (expired); USA 17 U.S.C. § 304 (pre-1929 public domain)",
                translation_status=TranslationRightsStatus.ORIGINAL_LANGUAGE,
                jurisdiction_basis="FR / USA",
                edition_basis="Paris 1903 first edition (French)",
                canonical_url="https://gallica.bnf.fr/ark:/12148/bpt6k65768837",
                production_rights_approved=False,
            ),
        ],
    },
    {
        "author": RecipeAuthor(
            author_id=AUTHOR_JAMIE_OLIVER,
            display_name="Джейми Оливер",
            bio="Британский шеф-повар, ресторатор и автор кулинарных книг.",
            created_at="2026-09-21 00:00:00",
        ),
        "sources": [
            RecipeSource(
                source_id="SRC_JAMIE_OLIVER_15MIN",
                author_id=AUTHOR_JAMIE_OLIVER,
                title="15-Minute Meals",
                source_type=SourceType.BOOK,
                source_locator="Michael Joseph, 2012",
                publication_year=2012,
                edition="1st edition, Michael Joseph / Penguin Books",
                language="en",
                rights_status=RightsStatus.LINK_ONLY,
                rights_evidence_type=RightsEvidenceType.LINK_ONLY_CITATION,
                rights_evidence_locator="ISBN 978-0718157807",
                rights_evidence_note="Copyright protected under UK CDPA 1988 (living author); LINK_ONLY metadata citation only; no structured recipe content license.",
                source_content_hash="meta_only_jamie_15min",
                ingestion_timestamp="2026-09-21 00:00:00",
                ingestion_tool_version="1.0.0",
                content_scope=ContentScope.METADATA_ONLY,
                verification_status=VerificationStatus.VERIFIED,
                created_at="2026-09-21 00:00:00",
            ),
        ],
    },
]

SRC_ESCOFFIER_1907_EN_SOURCE = RecipeSource(
    source_id="SRC_ESCOFFIER_1907_EN",
    author_id=AUTHOR_ESCOFFIER,
    title="A Guide to Modern Cookery (1907 English Edition)",
    source_type=SourceType.BOOK,
    source_locator="William Heinemann, London, 1907 / Project Gutenberg eBook #71395",
    publication_year=1907,
    edition="First English translation, William Heinemann, London",
    language="en",
    rights_status=RightsStatus.PUBLIC_DOMAIN,
    rights_evidence_type=RightsEvidenceType.PUBLIC_DOMAIN_STATUTE,
    rights_evidence_locator="Project Gutenberg eBook #71395 (pg71395.txt)",
    rights_evidence_note=(
        "Underlying work public domain; 1907 Heinemann English translation anonymous "
        "(UK CDPA 1988 s.12(3) expired 1978; US 17 U.S.C. § 304 pre-1929); "
        "Project Gutenberg eBook #71395 is public domain in the USA. "
        "Commercial reuse outside USA and under Project Gutenberg trademark requires legal review; "
        "offline pilot only; not approved for production deployment."
    ),
    source_content_hash="e0850c1d4589b8e03a7832258f911c447fb3dc0d9e706403822a7477c8615993",
    ingestion_timestamp="2026-09-21 00:00:00",
    ingestion_tool_version="1.0.0",
    content_scope=ContentScope.STRUCTURED_RECIPE_CONTENT,
    verification_status=VerificationStatus.VERIFIED,
    created_at="2026-09-21 00:00:00",
    underlying_work_rights="PUBLIC_DOMAIN",
    digital_reproduction_reuse_terms=(
        "Project Gutenberg License: public domain in USA; "
        "non-US distribution and commercial reuse require local law verification."
    ),
    commercial_reuse_status=CommercialReuseStatus.REVIEW_REQUIRED,
    partner_institution_terms="Project Gutenberg Literary Archive Foundation",
    target_jurisdiction_status="USA: Public domain (17 U.S.C. § 304); UK: Translation expired (CDPA 1988 s.12(3)); Other jurisdictions: Review required",
    translation_status=TranslationRightsStatus.PUBLIC_DOMAIN,
    jurisdiction_basis="USA / UK",
    edition_basis="William Heinemann, London, 1907",
    canonical_url="https://www.gutenberg.org/ebooks/71395",
    production_rights_approved=False,
)

SRC_ESCOFFIER_1903_COMMONS_SOURCE = RecipeSource(
    source_id="SRC_ESCOFFIER_1903_COMMONS",
    author_id=AUTHOR_ESCOFFIER,
    title="Le Guide Culinaire (1903/1907 Second Edition, French Original)",
    source_type=SourceType.BOOK,
    source_locator="Deuxième édition, Paris, 1907 / Wikimedia Commons File:Auguste Escoffier - Le Guide Culinaire - Aide-mémoire de cuisine pratique, 1903.djvu / Internet Archive b21525912",
    publication_year=1903,
    edition="Deuxième édition (1907) / Première édition (1903)",
    language="fr",
    rights_status=RightsStatus.PUBLIC_DOMAIN,
    rights_evidence_type=RightsEvidenceType.PUBLIC_DOMAIN_STATUTE,
    rights_evidence_locator="Wikimedia Commons File:Auguste Escoffier - Le Guide Culinaire - Aide-mémoire de cuisine pratique, 1903.djvu / Internet Archive b21525912",
    rights_evidence_note=(
        "Underlying work public domain worldwide (author Auguste Escoffier d. 1935, collaborator Philéas Gilbert d. 1942; "
        "published pre-1928, >70y pma expired). Digital scan published by Leeds University Library / Wellcome under Public Domain Mark 1.0; "
        "own extraction from digital image/text layer (transcription rights not applicable); commercial reuse permitted; approved for production."
    ),
    source_content_hash="e030f727e3e28102a02b46a2dc60a8ba342bc150deafbc02fd0d130d52e0dab9",
    ingestion_timestamp="2026-09-24 00:00:00",
    ingestion_tool_version="1.0.0",
    content_scope=ContentScope.STRUCTURED_RECIPE_CONTENT,
    verification_status=VerificationStatus.VERIFIED,
    created_at="2026-09-24 00:00:00",
    underlying_work_rights="PUBLIC_DOMAIN",
    digital_reproduction_rights="PUBLIC_DOMAIN_MARKED",
    digital_reproduction_reuse_terms="Public Domain Mark 1.0; scan published by Leeds University Library / Wellcome on Wikimedia Commons and Internet Archive",
    commercial_reuse_status=CommercialReuseStatus.COMMERCIAL_ALLOWED,
    transcription_source="OWN_EXTRACTION",
    transcription_rights="NOT_APPLICABLE",
    partner_institution_terms="Leeds University Library / Wellcome Collection (digital scan b21525912)",
    target_jurisdiction_status="Worldwide Public Domain (author d. 1935, collaborator d. 1942, published 1903/1907)",
    translation_status=TranslationRightsStatus.ORIGINAL_LANGUAGE,
    jurisdiction_basis="Worldwide / FR / US / UK",
    edition_basis="Paris 1903/1907 French second edition",
    canonical_url="https://commons.wikimedia.org/wiki/File:Auguste_Escoffier_-_Le_Guide_Culinaire_-_Aide-m%C3%A9moire_de_cuisine_pratique,_1903.djvu",
    production_rights_approved=True,
)

# Synthetic Public-Domain / Authorized Recipe Fixtures for Tests
# Contains 27 diverse recipes (9 breakfast, 9 lunch, 9 dinner) across 3 authors
_TEST_RECIPES_RAW = [
    # --- TEST AUTHOR A (9 recipes) ---
    {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A",
        "title": "Северная овсяная каша с лесными ягодами",
        "meal_types": [MealType.BREAKFAST],
        "cuisine": "Русская",
        "servings": 2,
        "prep_minutes": 5,
        "cook_minutes": 15,
        "total_minutes": 20,
        "difficulty": "easy",
        "tags": ["каша", "завтрак", "ягоды", "полезно"],
        "ingredients": [
            {"display_name": "овсяные хлопья", "quantity": 100, "unit": "g"},
            {"display_name": "молоко", "quantity": 250, "unit": "ml"},
            {"display_name": "ягоды", "quantity": 50, "unit": "g"},
            {"display_name": "сливочное масло", "quantity": 15, "unit": "g", "optional": True},
        ],
        "instructions": [{"step_number": 1, "text": "Вскипятить молоко и всыпать овсянку."}],
    },
    {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A",
        "title": "Омлет деревенский с зеленью",
        "meal_types": [MealType.BREAKFAST],
        "cuisine": "Русская",
        "servings": 2,
        "prep_minutes": 5,
        "cook_minutes": 10,
        "total_minutes": 15,
        "difficulty": "easy",
        "tags": ["омлет", "яйца", "завтрак", "белок"],
        "ingredients": [
            {"display_name": "яйца", "quantity": 4, "unit": "piece"},
            {"display_name": "молоко", "quantity": 50, "unit": "ml"},
            {"display_name": "сливочное масло", "quantity": 10, "unit": "g"},
            {"display_name": "соль", "quantity": 2, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Взбить яйца с молоком и солью, вылить на сковороду."}],
    },
    {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A",
        "title": "Гречневая каша рассыпчатая с грибами",
        "meal_types": [MealType.BREAKFAST],
        "cuisine": "Русская",
        "servings": 3,
        "prep_minutes": 10,
        "cook_minutes": 25,
        "total_minutes": 35,
        "difficulty": "easy",
        "tags": ["гречка", "завтрак", "веган", "пост"],
        "ingredients": [
            {"display_name": "гречневая крупа", "quantity": 200, "unit": "g"},
            {"display_name": "растительное масло", "quantity": 20, "unit": "ml"},
            {"display_name": "лук", "quantity": 1, "unit": "piece"},
            {"display_name": "соль", "quantity": 3, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Сварить гречку в соотношении 1:2 с водой."}],
    },
    {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A",
        "title": "Щи суточные с квашеной капустой",
        "meal_types": [MealType.LUNCH],
        "cuisine": "Русская",
        "servings": 4,
        "prep_minutes": 20,
        "cook_minutes": 60,
        "total_minutes": 80,
        "difficulty": "medium",
        "tags": ["суп", "обед", "капуста", "традиции"],
        "ingredients": [
            {"display_name": "капуста", "quantity": 300, "unit": "g"},
            {"display_name": "говядина", "quantity": 400, "unit": "g"},
            {"display_name": "картофель", "quantity": 3, "unit": "piece"},
            {"display_name": "морковь", "quantity": 1, "unit": "piece"},
            {"display_name": "лук", "quantity": 1, "unit": "piece"},
        ],
        "instructions": [{"step_number": 1, "text": "Сварить говяжий бульон, добавить капусту и картофель."}],
    },
    {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A",
        "title": "Уха из белой рыбы с луком",
        "meal_types": [MealType.LUNCH],
        "cuisine": "Русская",
        "servings": 4,
        "prep_minutes": 15,
        "cook_minutes": 30,
        "total_minutes": 45,
        "difficulty": "easy",
        "tags": ["суп", "обед", "рыба", "уха"],
        "ingredients": [
            {"display_name": "треска", "quantity": 500, "unit": "g"},
            {"display_name": "картофель", "quantity": 3, "unit": "piece"},
            {"display_name": "лук", "quantity": 1, "unit": "piece"},
            {"display_name": "черный перец", "quantity": 2, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Варить рыбу на медленном огне с овощами."}],
    },
    {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A",
        "title": "Рассольник с перловкой",
        "meal_types": [MealType.LUNCH],
        "cuisine": "Русская",
        "servings": 4,
        "prep_minutes": 20,
        "cook_minutes": 45,
        "total_minutes": 65,
        "difficulty": "medium",
        "tags": ["суп", "обед", "рассольник"],
        "ingredients": [
            {"display_name": "говядина", "quantity": 350, "unit": "g"},
            {"display_name": "картофель", "quantity": 2, "unit": "piece"},
            {"display_name": "огурцы", "quantity": 2, "unit": "piece"},
            {"display_name": "морковь", "quantity": 1, "unit": "piece"},
        ],
        "instructions": [{"step_number": 1, "text": "Сварить мясо, добавить крупу и овощи."}],
    },
    {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A",
        "title": "Куриные котлеты с картофельным пюре",
        "meal_types": [MealType.DINNER],
        "cuisine": "Русская",
        "servings": 3,
        "prep_minutes": 20,
        "cook_minutes": 30,
        "total_minutes": 50,
        "difficulty": "medium",
        "tags": ["ужин", "курица", "котлеты", "пюре"],
        "ingredients": [
            {"display_name": "куриная грудка", "quantity": 400, "unit": "g"},
            {"display_name": "картофель", "quantity": 500, "unit": "g"},
            {"display_name": "молоко", "quantity": 100, "unit": "ml"},
            {"display_name": "сливочное масло", "quantity": 30, "unit": "g"},
            {"display_name": "яйцо", "quantity": 1, "unit": "piece"},
        ],
        "instructions": [{"step_number": 1, "text": "Сделать фарш из курицы, сформовать котлеты и обжарить."}],
    },
    {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A",
        "title": "Жаркое из говядины в горшочке",
        "meal_types": [MealType.DINNER],
        "cuisine": "Русская",
        "servings": 4,
        "prep_minutes": 20,
        "cook_minutes": 50,
        "total_minutes": 70,
        "difficulty": "medium",
        "tags": ["ужин", "говядина", "жаркое", "мясо"],
        "ingredients": [
            {"display_name": "говядина", "quantity": 500, "unit": "g"},
            {"display_name": "картофель", "quantity": 4, "unit": "piece"},
            {"display_name": "морковь", "quantity": 1, "unit": "piece"},
            {"display_name": "лук", "quantity": 1, "unit": "piece"},
            {"display_name": "растительное масло", "quantity": 20, "unit": "ml"},
        ],
        "instructions": [{"step_number": 1, "text": "Нарезать мясо и овощи кубиками, тушить в горшочке."}],
    },
    {
        "author_id": TEST_AUTHOR_A,
        "source_id": "SRC_TEST_A",
        "title": "Рыбная запеканка с овощами",
        "meal_types": [MealType.DINNER],
        "cuisine": "Русская",
        "servings": 3,
        "prep_minutes": 15,
        "cook_minutes": 35,
        "total_minutes": 50,
        "difficulty": "easy",
        "tags": ["ужин", "рыба", "запеканка"],
        "ingredients": [
            {"display_name": "треска", "quantity": 450, "unit": "g"},
            {"display_name": "картофель", "quantity": 3, "unit": "piece"},
            {"display_name": "морковь", "quantity": 1, "unit": "piece"},
            {"display_name": "сыр", "quantity": 60, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Выложить слоями рыбу и картофель, посыпать сыром и запечь."}],
    },

    # --- TEST AUTHOR B (9 recipes) ---
    {
        "author_id": TEST_AUTHOR_B,
        "source_id": "SRC_TEST_B",
        "title": "Классический французский омлет с маслом",
        "meal_types": [MealType.BREAKFAST],
        "cuisine": "Французская",
        "servings": 2,
        "prep_minutes": 5,
        "cook_minutes": 5,
        "total_minutes": 10,
        "difficulty": "medium",
        "tags": ["завтрак", "омлет", "яйца", "классика"],
        "ingredients": [
            {"display_name": "яйца", "quantity": 4, "unit": "piece"},
            {"display_name": "сливочное масло", "quantity": 25, "unit": "g"},
            {"display_name": "соль", "quantity": 2, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Взбить вилкой, быстро обжарить на сливочном масле, свернуть."}],
    },
    {
        "author_id": TEST_AUTHOR_B,
        "source_id": "SRC_TEST_B",
        "title": "Яйца бенедикт с голландским соусом",
        "meal_types": [MealType.BREAKFAST],
        "cuisine": "Французская",
        "servings": 2,
        "prep_minutes": 15,
        "cook_minutes": 10,
        "total_minutes": 25,
        "difficulty": "hard",
        "tags": ["завтрак", "яйца", "бенедикт", "соус"],
        "ingredients": [
            {"display_name": "яйца", "quantity": 4, "unit": "piece"},
            {"display_name": "сливочное масло", "quantity": 80, "unit": "g"},
            {"display_name": "сыр", "quantity": 40, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Сварить яйца пашот и приготовить эмульсионный соус."}],
    },
    {
        "author_id": TEST_AUTHOR_B,
        "source_id": "SRC_TEST_B",
        "title": "Круассан с сыром и маслом",
        "meal_types": [MealType.BREAKFAST],
        "cuisine": "Французская",
        "servings": 2,
        "prep_minutes": 5,
        "cook_minutes": 10,
        "total_minutes": 15,
        "difficulty": "easy",
        "tags": ["завтрак", "сыр", "выпечка"],
        "ingredients": [
            {"display_name": "мука", "quantity": 150, "unit": "g"},
            {"display_name": "сливочное масло", "quantity": 40, "unit": "g"},
            {"display_name": "сыр", "quantity": 80, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Подогреть и подать с сыром."}],
    },
    {
        "author_id": TEST_AUTHOR_B,
        "source_id": "SRC_TEST_B",
        "title": "Луковый суп гратине",
        "meal_types": [MealType.LUNCH],
        "cuisine": "Французская",
        "servings": 4,
        "prep_minutes": 15,
        "cook_minutes": 45,
        "total_minutes": 60,
        "difficulty": "medium",
        "tags": ["суп", "обед", "лук", "сыр"],
        "ingredients": [
            {"display_name": "лук", "quantity": 4, "unit": "piece"},
            {"display_name": "сливочное масло", "quantity": 40, "unit": "g"},
            {"display_name": "сыр", "quantity": 100, "unit": "g"},
            {"display_name": "говядина", "quantity": 250, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Карамелизировать лук на сливочном масле, добавить бульон."}],
    },
    {
        "author_id": TEST_AUTHOR_B,
        "source_id": "SRC_TEST_B",
        "title": "Куриный консоме с овощами брюнуаз",
        "meal_types": [MealType.LUNCH],
        "cuisine": "Французская",
        "servings": 4,
        "prep_minutes": 25,
        "cook_minutes": 50,
        "total_minutes": 75,
        "difficulty": "hard",
        "tags": ["суп", "обед", "консоме", "курица"],
        "ingredients": [
            {"display_name": "курица", "quantity": 500, "unit": "g"},
            {"display_name": "морковь", "quantity": 2, "unit": "piece"},
            {"display_name": "лук", "quantity": 1, "unit": "piece"},
            {"display_name": "яйцо", "quantity": 2, "unit": "piece"},
        ],
        "instructions": [{"step_number": 1, "text": "Осветлить бульон взбитым белком, процедить."}],
    },
    {
        "author_id": TEST_AUTHOR_B,
        "source_id": "SRC_TEST_B",
        "title": "Крем-суп из тыквы с пармезаном",
        "meal_types": [MealType.LUNCH],
        "cuisine": "Французская",
        "servings": 3,
        "prep_minutes": 15,
        "cook_minutes": 25,
        "total_minutes": 40,
        "difficulty": "easy",
        "tags": ["суп", "крем-суп", "обед"],
        "ingredients": [
            {"display_name": "кабачок", "quantity": 400, "unit": "g"},
            {"display_name": "молоко", "quantity": 150, "unit": "ml"},
            {"display_name": "сливочное масло", "quantity": 25, "unit": "g"},
            {"display_name": "сыр", "quantity": 50, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Отварить овощи, пробить блендером с молоком и сыром."}],
    },
    {
        "author_id": TEST_AUTHOR_B,
        "source_id": "SRC_TEST_B",
        "title": "Беф Бургиньон традиционный",
        "meal_types": [MealType.DINNER],
        "cuisine": "Французская",
        "servings": 4,
        "prep_minutes": 25,
        "cook_minutes": 90,
        "total_minutes": 115,
        "difficulty": "hard",
        "tags": ["ужин", "говядина", "франция", "тушеное"],
        "ingredients": [
            {"display_name": "говядина", "quantity": 600, "unit": "g"},
            {"display_name": "морковь", "quantity": 2, "unit": "piece"},
            {"display_name": "лук", "quantity": 2, "unit": "piece"},
            {"display_name": "чеснок", "quantity": 2, "unit": "piece"},
            {"display_name": "сливочное масло", "quantity": 30, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Обжарить мясо, долго тушить с овощами на тихом огне."}],
    },
    {
        "author_id": TEST_AUTHOR_B,
        "source_id": "SRC_TEST_B",
        "title": "Куриное филе в сливочно-чесночном соусе",
        "meal_types": [MealType.DINNER],
        "cuisine": "Французская",
        "servings": 3,
        "prep_minutes": 10,
        "cook_minutes": 20,
        "total_minutes": 30,
        "difficulty": "medium",
        "tags": ["ужин", "курица", "соус", "сливки"],
        "ingredients": [
            {"display_name": "куриная грудка", "quantity": 450, "unit": "g"},
            {"display_name": "молоко", "quantity": 100, "unit": "ml"},
            {"display_name": "чеснок", "quantity": 3, "unit": "piece"},
            {"display_name": "сливочное масло", "quantity": 20, "unit": "g"},
            {"display_name": "рис", "quantity": 150, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Обжарить грудку, влить соус и потомить 5 минут."}],
    },
    {
        "author_id": TEST_AUTHOR_B,
        "source_id": "SRC_TEST_B",
        "title": "Филе лосося с травами и овощами",
        "meal_types": [MealType.DINNER],
        "cuisine": "Французская",
        "servings": 2,
        "prep_minutes": 10,
        "cook_minutes": 15,
        "total_minutes": 25,
        "difficulty": "medium",
        "tags": ["ужин", "рыба", "лосось", "полезно"],
        "ingredients": [
            {"display_name": "лосось", "quantity": 350, "unit": "g"},
            {"display_name": "брокколи", "quantity": 200, "unit": "g"},
            {"display_name": "оливковое масло", "quantity": 15, "unit": "ml"},
            {"display_name": "соль", "quantity": 2, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Запечь лосось с брокколи в духовке при 180C."}],
    },

    # --- TEST AUTHOR C (9 recipes) ---
    {
        "author_id": TEST_AUTHOR_C,
        "source_id": "SRC_TEST_C",
        "title": "Средиземноморская шакшука с томатами",
        "meal_types": [MealType.BREAKFAST],
        "cuisine": "Средиземноморская",
        "servings": 2,
        "prep_minutes": 10,
        "cook_minutes": 15,
        "total_minutes": 25,
        "difficulty": "easy",
        "tags": ["завтрак", "шакшука", "яйца", "томаты"],
        "ingredients": [
            {"display_name": "яйца", "quantity": 3, "unit": "piece"},
            {"display_name": "помидоры", "quantity": 3, "unit": "piece"},
            {"display_name": "лук", "quantity": 1, "unit": "piece"},
            {"display_name": "оливковое масло", "quantity": 15, "unit": "ml"},
            {"display_name": "чеснок", "quantity": 1, "unit": "piece"},
        ],
        "instructions": [{"step_number": 1, "text": "Обжарить лук и томаты, вбить яйца и накрыть крышкой."}],
    },
    {
        "author_id": TEST_AUTHOR_C,
        "source_id": "SRC_TEST_C",
        "title": "Тост с сыром, яйцом и огурцом",
        "meal_types": [MealType.BREAKFAST],
        "cuisine": "Средиземноморская",
        "servings": 2,
        "prep_minutes": 5,
        "cook_minutes": 5,
        "total_minutes": 10,
        "difficulty": "easy",
        "tags": ["завтрак", "тост", "сыр", "быстро"],
        "ingredients": [
            {"display_name": "мука", "quantity": 100, "unit": "g"},
            {"display_name": "сыр", "quantity": 50, "unit": "g"},
            {"display_name": "яйцо", "quantity": 2, "unit": "piece"},
            {"display_name": "огурцы", "quantity": 1, "unit": "piece"},
        ],
        "instructions": [{"step_number": 1, "text": "Собрать свежий тост с овощами и яйцом."}],
    },
    {
        "author_id": TEST_AUTHOR_C,
        "source_id": "SRC_TEST_C",
        "title": "Творожная запеканка с яблоком",
        "meal_types": [MealType.BREAKFAST],
        "cuisine": "Средиземноморская",
        "servings": 3,
        "prep_minutes": 10,
        "cook_minutes": 30,
        "total_minutes": 40,
        "difficulty": "easy",
        "tags": ["завтрак", "творог", "запеканка", "яблоко"],
        "ingredients": [
            {"display_name": "творог", "quantity": 300, "unit": "g"},
            {"display_name": "яйцо", "quantity": 2, "unit": "piece"},
            {"display_name": "яблоко", "quantity": 1, "unit": "piece"},
            {"display_name": "сахар", "quantity": 20, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Смешать творог с яйцами и яблоком, запечь в форме."}],
    },
    {
        "author_id": TEST_AUTHOR_C,
        "source_id": "SRC_TEST_C",
        "title": "Томатный суп Гаспачо освежающий",
        "meal_types": [MealType.LUNCH],
        "cuisine": "Средиземноморская",
        "servings": 3,
        "prep_minutes": 15,
        "cook_minutes": 0,
        "total_minutes": 15,
        "difficulty": "easy",
        "tags": ["суп", "обед", "гаспачо", "томаты", "холодный"],
        "ingredients": [
            {"display_name": "помидоры", "quantity": 4, "unit": "piece"},
            {"display_name": "огурцы", "quantity": 2, "unit": "piece"},
            {"display_name": "чеснок", "quantity": 1, "unit": "piece"},
            {"display_name": "оливковое масло", "quantity": 25, "unit": "ml"},
            {"display_name": "соль", "quantity": 3, "unit": "g"},
        ],
        "instructions": [{"step_number": 1, "text": "Измельчить свежие овощи блендером, заправить оливковым маслом."}],
    },
    {
        "author_id": TEST_AUTHOR_C,
        "source_id": "SRC_TEST_C",
        "title": "Суп минестроне с овощами и пастой",
        "meal_types": [MealType.LUNCH],
        "cuisine": "Средиземноморская",
        "servings": 4,
        "prep_minutes": 15,
        "cook_minutes": 25,
        "total_minutes": 40,
        "difficulty": "easy",
        "tags": ["суп", "обед", "паста", "овощи"],
        "ingredients": [
            {"display_name": "паста", "quantity": 100, "unit": "g"},
            {"display_name": "кабачок", "quantity": 1, "unit": "piece"},
            {"display_name": "морковь", "quantity": 1, "unit": "piece"},
            {"display_name": "помидоры", "quantity": 2, "unit": "piece"},
            {"display_name": "оливковое масло", "quantity": 15, "unit": "ml"},
        ],
        "instructions": [{"step_number": 1, "text": "Сварить легкий овощной суп с добавлением мелкой пасты."}],
    },
    {
        "author_id": TEST_AUTHOR_C,
        "source_id": "SRC_TEST_C",
        "title": "Салат с тунцом и отварным яйцом",
        "meal_types": [MealType.LUNCH],
        "cuisine": "Средиземноморская",
        "servings": 2,
        "prep_minutes": 10,
        "cook_minutes": 10,
        "total_minutes": 20,
        "difficulty": "easy",
        "tags": ["обед", "салат", "рыба", "яйца"],
        "ingredients": [
            {"display_name": "рыба", "quantity": 200, "unit": "g"},
            {"display_name": "яйца", "quantity": 2, "unit": "piece"},
            {"display_name": "огурцы", "quantity": 2, "unit": "piece"},
            {"display_name": "помидоры", "quantity": 2, "unit": "piece"},
            {"display_name": "оливковое масло", "quantity": 15, "unit": "ml"},
        ],
        "instructions": [{"step_number": 1, "text": "Смешать нарезанные овощи с рыбой и яйцом."}],
    },
    {
        "author_id": TEST_AUTHOR_C,
        "source_id": "SRC_TEST_C",
        "title": "Паста с курицей и помидорами черри",
        "meal_types": [MealType.DINNER],
        "cuisine": "Средиземноморская",
        "servings": 3,
        "prep_minutes": 10,
        "cook_minutes": 15,
        "total_minutes": 25,
        "difficulty": "easy",
        "tags": ["ужин", "паста", "курица", "томаты"],
        "ingredients": [
            {"display_name": "паста", "quantity": 250, "unit": "g"},
            {"display_name": "куриная грудка", "quantity": 350, "unit": "g"},
            {"display_name": "помидоры", "quantity": 3, "unit": "piece"},
            {"display_name": "чеснок", "quantity": 2, "unit": "piece"},
            {"display_name": "оливковое масло", "quantity": 20, "unit": "ml"},
        ],
        "instructions": [{"step_number": 1, "text": "Отварить пасту аль денте, перемешать с обжаренной курицей."}],
    },
    {
        "author_id": TEST_AUTHOR_C,
        "source_id": "SRC_TEST_C",
        "title": "Запеченная треска с картофелем и розмарином",
        "meal_types": [MealType.DINNER],
        "cuisine": "Средиземноморская",
        "servings": 3,
        "prep_minutes": 15,
        "cook_minutes": 30,
        "total_minutes": 45,
        "difficulty": "easy",
        "tags": ["ужин", "треска", "рыба", "картофель"],
        "ingredients": [
            {"display_name": "треска", "quantity": 400, "unit": "g"},
            {"display_name": "картофель", "quantity": 4, "unit": "piece"},
            {"display_name": "оливковое масло", "quantity": 20, "unit": "ml"},
            {"display_name": "чеснок", "quantity": 2, "unit": "piece"},
        ],
        "instructions": [{"step_number": 1, "text": "Запечь картофель с рыбой под оливковым маслом."}],
    },
    {
        "author_id": TEST_AUTHOR_C,
        "source_id": "SRC_TEST_C",
        "title": "Овощное рагу с курицей и кабачком",
        "meal_types": [MealType.DINNER],
        "cuisine": "Средиземноморская",
        "servings": 4,
        "prep_minutes": 15,
        "cook_minutes": 25,
        "total_minutes": 40,
        "difficulty": "easy",
        "tags": ["ужин", "курица", "рагу", "кабачок"],
        "ingredients": [
            {"display_name": "куриная грудка", "quantity": 400, "unit": "g"},
            {"display_name": "кабачок", "quantity": 2, "unit": "piece"},
            {"display_name": "помидоры", "quantity": 2, "unit": "piece"},
            {"display_name": "морковь", "quantity": 1, "unit": "piece"},
            {"display_name": "растительное масло", "quantity": 15, "unit": "ml"},
        ],
        "instructions": [{"step_number": 1, "text": "Тушить курицу с сезонными овощами до готовности."}],
    },
]


def build_test_recipe_catalog(db_path: str | Path, *, build_id: str = "test-catalog-v1") -> str:
    """Builds a verified test catalog with 27 safe recipes across 3 neutral test authors."""
    content_hash, _ = build_pilot_recipe_catalog(db_path, build_id=build_id)
    return content_hash


def build_pilot_recipe_catalog(
    db_path: str | Path,
    *,
    build_id: str = "pilot-v1",
    reports_dir: str | Path | None = None,
    overwrite: bool = True,
) -> tuple[str, CatalogReadinessReport]:
    """Builds the canonical offline pilot catalog and computes deterministic readiness."""
    path = Path(db_path)
    if overwrite and path.exists():
        path.unlink()
    pipeline = RecipeIngestionPipeline(path, build_id=build_id)

    # 1. Ingest production metadata authors and sources
    for manifest in PRODUCTION_MANIFESTS:
        pipeline.ingest_author(manifest["author"])
        for src in manifest["sources"]:
            pipeline.ingest_source(src)

    # 2. Ingest test authors and sources
    for author_id, name in TEST_AUTHORS.items():
        pipeline.ingest_author(
            RecipeAuthor(author_id=author_id, display_name=name, created_at="2026-09-21 00:00:00")
        )
        src_id = f"SRC_{author_id.replace('AUTHOR_', '')}"
        pipeline.ingest_source(
            RecipeSource(
                source_id=src_id,
                author_id=author_id,
                title=f"Открытый сборник проверенных домашних блюд ({name})",
                source_type=SourceType.USER_DOCUMENT,
                source_locator="Тестовый набор HealBite v1, p. 1",
                publication_year=2026,
                edition="1-е издание",
                language="ru",
                rights_status=RightsStatus.USER_PROVIDED_AUTHORIZED,
                rights_evidence_type=RightsEvidenceType.OPERATOR_USER_GRANT,
                rights_evidence_locator="internal://tests/fixtures/synthetic_recipes_v1",
                rights_evidence_note="Synthetic test recipes granted by repository operator for test validation only.",
                source_content_hash=f"hash_{src_id}",
                ingestion_timestamp="2026-09-21 00:00:00",
                ingestion_tool_version="1.0.0",
                content_scope=ContentScope.STRUCTURED_RECIPE_CONTENT,
                verification_status=VerificationStatus.VERIFIED,
                created_at="2026-09-21 00:00:00",
            )
        )

    # 3. Ingest recipes
    for recipe_data in _TEST_RECIPES_RAW:
        data = dict(recipe_data)
        data["rights_status"] = RightsStatus.USER_PROVIDED_AUTHORIZED.value
        data["verified"] = True
        data["verification_status"] = VerificationStatus.VERIFIED.value
        data.setdefault("source_locator", "Тестовый набор HealBite v1, p. 1")
        pipeline.ingest_recipe(data)

    dup_report = pipeline.get_duplicate_report()
    content_hash = pipeline.finalize_catalog()
    pipeline.close()

    # Reopen and validate
    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)
    readiness = calculate_catalog_readiness(store)

    if reports_dir is not None:
        r_path = Path(reports_dir)
        r_path.mkdir(parents=True, exist_ok=True)

        # Write duplicates report
        dup_dict = {
            "exact_duplicates": list(dup_report.exact_duplicates),
            "normalized_duplicates": list(dup_report.normalized_duplicates),
            "conflicting_variants": list(dup_report.conflicting_variants),
        }
        (r_path / "duplicates_report.json").write_text(
            json.dumps(dup_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        # Write author coverage report
        cov_dict = {
            aid: {
                "author_id": cov.author_id,
                "display_name": cov.display_name,
                "metadata_sources": cov.metadata_sources,
                "structured_authorized_sources": cov.structured_authorized_sources,
                "verified_recipes": cov.verified_recipes,
                "blocked_sources": cov.blocked_sources,
                "block_reasons": list(cov.block_reasons),
                "breakfast_verified": cov.breakfast_verified,
                "lunch_verified": cov.lunch_verified,
                "dinner_verified": cov.dinner_verified,
                "unique_verified_recipes": cov.unique_verified_recipes,
                "can_form_21_meal_week": cov.can_form_21_meal_week,
            }
            for aid, cov in readiness.author_coverages.items()
        }
        (r_path / "author_coverage_report.json").write_text(
            json.dumps(cov_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        # Write catalog build report
        build_dict = {
            "schema_version": readiness.schema_version,
            "build_id": readiness.build_id,
            "content_hash": readiness.content_hash,
            "recipe_count": readiness.recipe_count,
            "verified_recipe_count": readiness.verified_recipe_count,
            "real_verified_recipes": readiness.real_verified_recipes,
            "test_fixture_recipes": readiness.test_fixture_recipes,
            "breakfast_verified": readiness.breakfast_verified,
            "lunch_verified": readiness.lunch_verified,
            "dinner_verified": readiness.dinner_verified,
            "unique_verified_recipes": readiness.unique_verified_recipes,
            "can_form_21_meal_week": readiness.can_form_21_meal_week,
        }
        (r_path / "catalog_build_report.json").write_text(
            json.dumps(build_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    return content_hash, readiness


def build_escoffier_real_catalog(
    db_path: str | Path,
    *,
    build_id: str = "escoffier-commons-v1",
    reports_dir: str | Path | None = None,
    recipes_json_path: str | Path = "recipe_corpus/authorized_inputs/escoffier_recipes.json",
    include_test_fixtures: bool = False,
) -> tuple[str, CatalogReadinessReport]:
    p_path = Path(db_path)
    if p_path.exists():
        p_path.unlink()
    pipeline = RecipeIngestionPipeline(p_path, build_id=build_id)

    # 1. Ingest production manifests (authors and metadata-only sources)
    for manifest in PRODUCTION_MANIFESTS:
        pipeline.ingest_author(manifest["author"])
        for source in manifest["sources"]:
            pipeline.ingest_source(source)

    # 2. Ingest authorized Escoffier 1903 Commons French source
    pipeline.ingest_source(SRC_ESCOFFIER_1903_COMMONS_SOURCE)

    # 3. Optionally ingest test fixtures (for hybrid testing)
    if include_test_fixtures:
        for author_id, name in TEST_AUTHORS.items():
            pipeline.ingest_author(
                RecipeAuthor(author_id=author_id, display_name=name, created_at="2026-09-21 00:00:00")
            )
            src_id = f"SRC_{author_id.replace('AUTHOR_', '')}"
            pipeline.ingest_source(
                RecipeSource(
                    source_id=src_id,
                    author_id=author_id,
                    title=f"Открытый сборник проверенных домашних блюд ({name})",
                    source_type=SourceType.USER_DOCUMENT,
                    source_locator="Тестовый набор HealBite v1, p. 1",
                    publication_year=2026,
                    edition="1-е издание",
                    language="ru",
                    rights_status=RightsStatus.USER_PROVIDED_AUTHORIZED,
                    rights_evidence_type=RightsEvidenceType.OPERATOR_USER_GRANT,
                    rights_evidence_locator="internal://tests/fixtures/synthetic_recipes_v1",
                    rights_evidence_note="Synthetic test recipes granted by repository operator for test validation only.",
                    source_content_hash=f"hash_{src_id}",
                    ingestion_timestamp="2026-09-21 00:00:00",
                    ingestion_tool_version="1.0.0",
                    content_scope=ContentScope.STRUCTURED_RECIPE_CONTENT,
                    verification_status=VerificationStatus.VERIFIED,
                    created_at="2026-09-21 00:00:00",
                )
            )
        for recipe_data in _TEST_RECIPES_RAW:
            data = dict(recipe_data)
            data["rights_status"] = RightsStatus.USER_PROVIDED_AUTHORIZED.value
            data["verified"] = True
            data["verification_status"] = VerificationStatus.VERIFIED.value
            data.setdefault("source_locator", "Тестовый набор HealBite v1, p. 1")
            pipeline.ingest_recipe(data)

    # 4. Ingest real Escoffier recipes from JSON
    r_path = Path(recipes_json_path)
    if not r_path.is_absolute():
        r_path = REPO_ROOT / r_path
    if r_path.exists():
        raw_recipes = json.loads(r_path.read_text(encoding="utf-8"))
        for item in raw_recipes:
            pipeline.ingest_recipe(item)

    dup_report = pipeline.get_duplicate_report()
    content_hash = pipeline.finalize_catalog()
    pipeline.close()

    # Reopen and validate
    store = HealBiteRecipeCatalogStore(db_path, read_only=True, validate_hash=True)
    readiness = calculate_catalog_readiness(store)

    if reports_dir is not None:
        rep_dir = Path(reports_dir)
        rep_dir.mkdir(parents=True, exist_ok=True)

        dup_dict = {
            "exact_duplicates": list(dup_report.exact_duplicates),
            "normalized_duplicates": list(dup_report.normalized_duplicates),
            "conflicting_variants": list(dup_report.conflicting_variants),
        }
        (rep_dir / "duplicates_report.json").write_text(
            json.dumps(dup_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        cov_dict = {
            aid: {
                "author_id": cov.author_id,
                "display_name": cov.display_name,
                "metadata_sources": cov.metadata_sources,
                "structured_authorized_sources": cov.structured_authorized_sources,
                "verified_recipes": cov.verified_recipes,
                "blocked_sources": cov.blocked_sources,
                "block_reasons": list(cov.block_reasons),
                "breakfast_verified": cov.breakfast_verified,
                "lunch_verified": cov.lunch_verified,
                "dinner_verified": cov.dinner_verified,
                "unique_verified_recipes": cov.unique_verified_recipes,
                "can_form_21_meal_week": cov.can_form_21_meal_week,
                "production_canary_eligible": cov.production_canary_eligible,
            }
            for aid, cov in readiness.author_coverages.items()
        }
        (rep_dir / "author_coverage_report.json").write_text(
            json.dumps(cov_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        build_dict = {
            "schema_version": readiness.schema_version,
            "build_id": readiness.build_id,
            "content_hash": readiness.content_hash,
            "recipe_count": readiness.recipe_count,
            "verified_recipe_count": readiness.verified_recipe_count,
            "real_verified_recipes": readiness.real_verified_recipes,
            "test_fixture_recipes": readiness.test_fixture_recipes,
            "breakfast_verified": readiness.breakfast_verified,
            "lunch_verified": readiness.lunch_verified,
            "dinner_verified": readiness.dinner_verified,
            "unique_verified_recipes": readiness.unique_verified_recipes,
            "can_form_21_meal_week": readiness.can_form_21_meal_week,
            "production_canary_eligible": readiness.production_canary_eligible,
        }
        (rep_dir / "catalog_build_report.json").write_text(
            json.dumps(build_dict, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    return content_hash, readiness
