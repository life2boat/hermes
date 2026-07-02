# HealBite Weekly Menu Data Contracts

Status: design-only proposal

## Scope

This document defines proposed Markdown-only contracts for household-aware
weekly meal planning. It does not add migrations, prompt builders, runtime
handlers, or LLM calls.

## Core Entities

### Weekly Meal Plan

Proposed fields:

```text
id
household_id
week_start
timezone
status
generation_version
nutrition_snapshot_version
request_fingerprint
generation_attempt
failure_code
created_by_user_id
created_at
updated_at
finalized_at
archived_at
version
```

Statuses:

```text
draft
generating
validating
ready
failed
superseded
archived
```

### Meal Plan Day

```text
id
weekly_plan_id
local_date
day_index
calculated_nutrition_summary
version
```

### Planned Meal

```text
id
meal_plan_day_id
meal_slot
recipe_version_id
custom_title
status
source
replacement_of_id
created_at
updated_at
version
```

Planned meal state must not be mixed with consumed diary state.

### Member Meal Allocation

```text
id
planned_meal_id
household_member_id
portion_quantity
portion_unit
recipe_yield_fraction
target_calories
calculated_calories
calculated_protein
calculated_fat
calculated_carbs
status
version
```

Authoritative portion representation: `recipe_yield_fraction` plus display
quantity. The fraction is stable for deterministic nutrition math. Display
quantity is retained for UX.

## Recipes

Separate immutable-ish recipe identity from versioned content.

### Recipe

```text
id
household_id nullable
source_type
canonical_title
status
created_by_user_id nullable
created_at
updated_at
version
```

### Recipe Version

```text
id
recipe_id
version_number
title
yield_quantity
yield_unit
instructions_json
nutrition_summary
source_generation_id nullable
created_at
status
```

### Recipe Ingredient

```text
id
recipe_version_id
canonical_food_id nullable
source_name
source_quantity
source_unit
normalized_quantity
normalized_unit
conversion_method
conversion_confidence
preparation_note nullable
sort_order
```

Do not store mutable recipe history as one constantly overwritten JSON blob.

## Canonical Food

Proposed fields:

```text
id
canonical_name
category
base_unit
density nullable
edible_fraction nullable
status
created_at
updated_at
version
```

Aliases may be stored separately. Products must not be automatically merged
only by fuzzy text match.

## Dietary Restrictions

Restriction classes:

- hard allergy;
- medical restriction;
- religious or ethical restriction;
- strong dislike;
- soft preference.

Hard allergies and medical restrictions are deterministic validation inputs.
They are not optional LLM preferences.

## LLM JSON Contract

LLM may propose meal variety, recipes, and replacements. LLM is not source of
truth for KBJU, portions, permissions, idempotency, or DB state.

```json
{
  "type": "object",
  "required": ["plan_days", "recipes"],
  "additionalProperties": false,
  "properties": {
    "plan_days": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["day_index", "meals"],
        "additionalProperties": false,
        "properties": {
          "day_index": {"type": "integer", "minimum": 0, "maximum": 6},
          "meals": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["meal_slot", "recipe_ref", "household_allocations"],
              "additionalProperties": false,
              "properties": {
                "meal_slot": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "snack"]},
                "recipe_ref": {"type": "string"},
                "household_allocations": {
                  "type": "array",
                  "items": {
                    "type": "object",
                    "required": ["member_label", "portion_hint"],
                    "additionalProperties": false,
                    "properties": {
                      "member_label": {"type": "string", "pattern": "^(member|dependent)_[0-9]+$"},
                      "portion_hint": {"type": "string"}
                    }
                  }
                }
              }
            }
          }
        }
      }
    },
    "recipes": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["recipe_ref", "title", "ingredients"],
        "additionalProperties": false,
        "properties": {
          "recipe_ref": {"type": "string"},
          "title": {"type": "string"},
          "ingredients": {"type": "array"}
        }
      }
    }
  }
}
```

Do not persist chain of thought or raw provider response.

## Deterministic Pipeline

```text
LLM draft
→ JSON validation
→ hard restriction validation
→ ingredient resolution
→ nutrition lookup
→ recipe nutrition calculation
→ member portion allocation
→ daily target comparison
→ correction/retry
→ atomic persistence
→ ready plan
```

A plan may become `ready` only after the full successful transaction commits.

## Nutrition Target Snapshot

Plan generation stores the member target versions used for calculation. A later
profile or target change does not silently recalculate old plans. Users may
explicitly regenerate or refresh a plan.

## State Machines

### Weekly Plan

```mermaid
stateDiagram-v2
  [*] --> draft
  draft --> generating: create_requested
  generating --> validating: llm_draft_received
  validating --> ready: validation_committed
  generating --> failed: provider_or_timeout_failure
  validating --> failed: validation_failure
  ready --> superseded: replacement_plan_ready
  ready --> archived: user_archives
  failed --> draft: retry_requested
  superseded --> archived
```

### Meal Replacement

```mermaid
stateDiagram-v2
  [*] --> requested
  requested --> generating
  generating --> validating
  validating --> applied: replacement_committed
  generating --> failed
  validating --> failed
```

## Idempotency and Concurrency

Use `request_fingerprint`, command `idempotency_key`, generation leases,
generation attempts, optimistic versions, unique constraints, and
compare-and-swap updates. Correlation ID must not be used as the business
idempotency key.

## Transaction Boundaries

Recommended atomic commits:

1. create plan shell and claim generation lease;
2. persist validated recipes, days, meals, allocations, and nutrition summaries;
3. mark plan `ready`;
4. generate or refresh shopping list from ready plan in a separate transaction.

If validation fails, plan remains `failed` with a safe failure code, not partial
ready content.

## Proposed Indexes and Constraints

```sql
CREATE UNIQUE INDEX weekly_plan_active_week
ON weekly_meal_plans(household_id, week_start)
WHERE status IN ('draft', 'generating', 'validating', 'ready');

CREATE UNIQUE INDEX meal_day_index
ON meal_plan_days(weekly_plan_id, day_index);

CREATE INDEX member_allocation_member
ON member_meal_allocations(household_member_id, planned_meal_id);
```

## Observability

Allowed safe markers:

```text
route
feature_state
household_size_bucket
plan_day_count
meal_count_bucket
generation_status
validation_outcome
retry_count_bucket
duration_bucket
provider_error_type
```

Forbidden in logs:

```text
Telegram IDs
household member names
allergies
medical restrictions
exact nutrition targets
meal titles
raw prompts
raw LLM responses
callback payloads
raw exception bodies
```

## Domain Test Matrix

| Scenario | Expected result |
| --- | --- |
| one-member household | plan uses primary member target snapshot |
| two-member household | shared meals get different allocations |
| hard allergy conflict | plan validation fails or retries |
| soft dislike conflict | can be used as ranking preference |
| meal replacement | only selected meal changes |
| provider timeout | no partial ready plan |
| double create click | one logical plan or idempotent response |
| concurrent replacement | one compare-and-swap winner |
| target changes after plan | old plan keeps snapshot |
| diary integration | planned meal does not auto-log consumed food |
## Plan Versioning Decision

Use immutable whole-plan versions as the source-of-truth model.

A `weekly_meal_plan` represents one immutable plan version once it reaches
`ready`. Regeneration creates a new plan version and marks the previous ready
plan `superseded`. Meal replacement creates a new immutable plan version derived
from the previous one, with unchanged days/meals copied forward and only the
selected meal replaced.

Mutable UI state may point to the current active plan version, but shopping
lists and reports reference the immutable plan version they were generated
from. `archived` means the user intentionally hides or closes a version.
`superseded` means a newer version replaced it.

## Nutrition Snapshot Source of Truth

Use explicit snapshot rows or a serialized snapshot document attached to the
plan generation transaction. The snapshot records member ID, target row ID,
target `version`, effective range, and target values used for calculation.

`nutrition_target.version` is the mutable member-target version. The plan-level
`nutrition_snapshot_version` is the immutable snapshot generation version. Ready
plans must read from the snapshot, not from current profile rows, so profile
edits do not rewrite historical plans.

## Meal Replacement Contract

A replacement operation:

1. verifies household boundary and current plan version;
2. creates a new replacement attempt with a business idempotency key;
3. generates and validates only the selected meal replacement;
4. preserves old meal as audit history through `replacement_of_id`;
5. copies unaffected days and meals into the new immutable plan version;
6. recalculates nutrition for the affected day and member allocations;
7. marks shopping lists derived from the old plan version `stale`;
8. commits plan version, allocations, nutrition summaries, and stale marker atomically.

Concurrent replacements use optimistic version compare-and-swap. One request
wins; later requests reload the current plan version and must retry explicitly.

## Recipe Versioning Addendum

`planned_meal.recipe_version_id` always points to a concrete recipe version.
`recipe` is identity and ownership; `recipe_version` is immutable content for a
specific title, yield, instructions, ingredients, and nutrition calculation.

Add design fields:

```text
recipe_version.content_hash
recipe_version.yield_quantity
recipe_version.yield_unit
recipe_ingredient.source_quantity
recipe_ingredient.source_unit
recipe_ingredient.normalized_quantity
recipe_ingredient.normalized_unit
```

System, LLM-proposed, and user-created recipes share the same versioning model.

## Portion Conservation Rule

For MVP, leftovers are an open product decision and are not implicitly modeled.
Therefore member allocations for a shared planned meal should either sum to the
recipe yield fraction used for the meal or explicitly record an unallocated
leftover fraction. Until leftovers are implemented, validation should reject
silent over-allocation and under-allocation that cannot be explained.

## LLM Contract Addendum

The generation result must include `schema_version`, recipe yield, and ingredient
quantities/units. Provider payload must use neutral labels such as `member_1`,
`member_2`, and `dependent_1`; it must not include Telegram IDs, application
user IDs, real household IDs, real names, weight history, water history, or raw
conversation history.

Additional required JSON shape:

```json
{
  "schema_version": "weekly_menu.v1",
  "recipes": [
    {
      "recipe_ref": "recipe_1",
      "title": "example",
      "yield": {"quantity": 4, "unit": "serving"},
      "ingredients": [
        {"name": "example ingredient", "quantity": 100, "unit": "g"}
      ]
    }
  ]
}
```

## Nutrition Validation Addendum

Existing HealBite deterministic nutrition calculations are the starting point
for future implementation. Sprint 7.1A does not assert medical correctness or
introduce hardcoded medical norms.

Tolerances, rounding rules, and maximum repair attempts are product/config
choices. Recommended defaults for implementation planning:

```text
target tolerance: configurable percent band
rounding: display-only, never storage-only
repair attempts: bounded, default max 2
missing nutrition data: validation warning/failure, never fake zero
```

A ready plan cannot be committed while required nutrition data is missing unless
the product explicitly accepts an incomplete-plan state in a later sprint.

## Constraint and Index Addendum

Future implementation should include or justify equivalents for:

```text
FOREIGN KEY weekly_meal_plans.household_id -> households.id
FOREIGN KEY meal_plan_days.weekly_plan_id -> weekly_meal_plans.id
FOREIGN KEY planned_meals.meal_plan_day_id -> meal_plan_days.id
FOREIGN KEY planned_meals.recipe_version_id -> recipe_versions.id
UNIQUE weekly plan active version per household/week where status is active
UNIQUE recipe_versions(recipe_id, version_number)
UNIQUE generation idempotency key per household/week/request fingerprint
INDEX weekly_meal_plans(household_id, week_start, status)
INDEX planned_meals(meal_plan_day_id, meal_slot)
INDEX member_meal_allocations(household_member_id, planned_meal_id)
```

SQLite partial indexes can enforce active-plan uniqueness, but cross-table
household consistency still requires service validation and tests because SQLite
cannot express every cross-household invariant as a simple foreign key.
