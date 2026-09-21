# ADR-0089: Source-Grounded Recipe Weekly Menu Planning

## Context

Previous iterations of weekly menu generation relied on LLM generative synthesis where dish titles, descriptions, and ingredients were produced free-form. This presented critical product and safety risks:
1. Hallucinated culinary procedures, unrealistic cooking times, or unbalanced nutritional structures.
2. False attribution of dishes to canonical authors (e.g. V. V. Pokhlebkin, Auguste Escoffier, Jamie Oliver) without source grounding.
3. Ingredient drift where dishes claimed ingredients not matching real culinary recipes or made arbitrary substitutions.
4. Risk of copyright infringement if model memorization leaked verbatim proprietary cookbook texts into chat outputs.

To ensure deterministic reliability, culinary authenticity, copyright compliance, and strict multi-tenant isolation, Hermes requires a source-grounded recipe architecture where weekly menus are planned exclusively from verified recipes in an immutable catalog.

---

## Decision

We establish the **Recipe-Grounded Weekly Menu Planning System (v1)** adhering to the following architectural decisions and contracts:

### 1. Invariants & Grounding Contract
- **Source of Truth**: The Canonical Recipe Catalog is authoritative truth. The LLM is an untrusted planner/ranker, never a creator of recipes.
- **Recipe Identification**: Every recipe possesses an immutable, deterministic `recipe_id` (UUIDv5) derived from its immutable identity tuple: `(author_id, source_id, source_locator, normalized_recipe_title)`.
- **Strict Grounding**: Every planned meal slot in a recipe-grounded weekly menu must reference an existing, verified `recipe_id` and `recipe_version`.
- **Full Coverage Requirement**: Publishing a recipe-grounded weekly menu requires exact `21/21` verified meal slots (`7 days × 3 meals/day`). `GROUNDING_RATIO == 1.0`.
- **Fail-Closed Policy**: If fewer than 21 verified recipes satisfy household dietary, allergy, or availability constraints, the system fails closed with `INSUFFICIENT_GROUNDED_RECIPES`. There is **zero** generative or free-form fallback.
- **Catalog Attribution**: Recipe titles, author names, book titles, and instructions are resolved exclusively from the catalog after validation. LLM text cannot supply author names or recipe titles.
- **Model Schema Restriction**: The LLM planner output schema accepts only: `(date, meal_slot, recipe_id, recipe_version, target_servings)`. It contains no title, author, or ingredient fields. Any unknown `recipe_id` results in immediate plan rejection.

### 2. Copyright & Rights Classification
Source material must carry an explicit rights classification per source edition:
- `PUBLIC_DOMAIN`: Supported by historical publication/edition evidence.
- `LICENSED`: Explicitly licensed structured content.
- `USER_PROVIDED_AUTHORIZED`: Operator or user-provided structured material authorized for processing.
- `LINK_ONLY`: Bibliographic metadata and external link reference only; full recipe instructions cannot be ingested.
- `UNKNOWN`: Ingestion is strictly rejected.

No copyrighted cookbook PDFs, scans, or raw verbatim corpora are checked into Git. Initial production manifests define metadata-only records for Pokhlebkin, Escoffier, and Jamie Oliver. Testing uses neutral, rights-safe synthetic and public-domain fixture catalogs.

### 3. Immutable Recipe Catalog Storage
- The recipe catalog resides in a dedicated SQLite database (`recipe_catalog.db`), strictly separated from mutable user/household data.
- The runtime opens the catalog in **read-only** mode. Runtime planning cannot insert, update, or delete recipes.
- The catalog header stores verification metadata:
  - `CATALOG_SCHEMA_VERSION`
  - `CATALOG_BUILD_ID`
  - `CATALOG_CONTENT_HASH`
  - `CATALOG_RECIPE_COUNT`
  - `CATALOG_VERIFIED_RECIPE_COUNT`
- If the content hash fails validation or the schema version is unsupported, catalog opening fails closed.

### 4. Deterministic Serving Scaling & Adaptations
- Serving scaling is purely deterministic application logic:
  $$\text{scaled\_quantity} = \text{original\_quantity} \times \frac{\text{target\_servings}}{\text{source\_servings}}$$
- The model is never permitted to calculate or scale ingredient quantities.
- Unknown quantities in recipes remain `UNKNOWN`.
- Substitutions are prohibited in v1 unless governed by an explicit, verified `RecipeAdaptation` record. If an ingredient is missing and has no verified adaptation, it is routed to the Shopping List.

### 5. Inventory & Shopping List Integration
- **Confirmed Inventory**: Matches available products against normalized recipe ingredients (`ingredient_id`).
- **Ranking vs. Modification**: Inventory overlap increases candidate ranking scores. Inventory state **never** rewrites or removes ingredients from a recipe.
- **Shopping Derivation**: Derived deterministically by aggregating scaled recipe ingredients across the 21 verified meals, subtracting available inventory for compatible units, and recording provenance with `origin='recipe_grounded'`.
- **Idempotency**: Shopping generation uses a deterministic idempotency key based on household, weekly menu revision, and catalog build ID.

### 6. Backward Compatibility & Additive Storage
- Provenance linkage in HealBite SQLite is additive via `household_weekly_menu_entry_recipe_refs` (`entry_id`, `recipe_id`, `recipe_version`, `source_id`, `author_id`, `target_servings`, `created_at`).
- Existing weekly menu generation, inventory-based menus, and shopping flows remain fully functional when recipe-grounded feature flags are disabled.
- Feature flags default to `false`:
  - `HEALBITE_RECIPE_CATALOG_ENABLED=false`
  - `HEALBITE_RECIPE_GROUNDED_MENU_ENABLED=false`
  - `HEALBITE_RECIPE_GROUNDED_MENU_PUBLIC=false`
  - `HEALBITE_RECIPE_GROUNDED_MENU_ALLOWLIST=""`
  - `HEALBITE_RECIPE_VECTOR_RETRIEVAL_ENABLED=false`

---

## Consequences

- **Positive**: Eliminates hallucinated food, fabricated cookbook attribution, and dietary drift. Ensures legal safety regarding copyrighted materials. Provides verifiable culinary plans.
- **Negative / Constraints**: Requires pre-ingested, verified recipe catalogs with sufficient diversity (at least 21 recipes matching constraints). Incomplete catalogs fail closed rather than producing a partial menu.
