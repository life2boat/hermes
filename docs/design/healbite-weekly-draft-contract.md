# Weekly draft success contract

Repository candidate based on `f0cb471a6102be21c5e101d2885a40bb727e6585`.
No runtime activation, schema change, migration, allowlist edit or production
data inspection is part of this change.

## Root cause and preserved entry points

Fast menu (`weekly_menu:v1:g:...`, `HealBiteWeeklyMenuTelegramController`)
and inventory menu (`HealBiteInventoryTelegramController._generate`) retain
their separate inputs and UI. Both use `HealBiteWeeklyMenuGenerationService`
and `AuxiliaryWeeklyMenuGenerator`; inventory supplies a confirmed snapshot.

Previously the Fast Menu system prompt omitted ingredients and its parser's
closed allowed-field set rejected `ingredients`. `WeeklyMenuGeneratedEntry`
therefore defaulted to an empty tuple. The generation-to-store conversion
iterated that tuple and stored no ingredient rows. Publication required only
one entry, so the missing data could become a published menu. The inventory
JSON contract already required seven days, three meals/day and ingredients,
but injected generators bypassed that parser-level protection.

## Common boundary

`gateway/healbite_weekly_menu_draft.py::validate_weekly_menu_draft` requires
exactly the canonical seven dates and breakfast/lunch/dinner once per date
(21 meals, position 1). Every meal has 1..32 ingredients, a nonempty title and
positive servings. Ingredient shape, units and finite positive decimal rules
reuse the existing weekly/inventory representation, not a new schema.

Fast Menu retains the `entries` wire shape and now explicitly requests and
preserves `ingredients: [{name, quantity_value, unit}]` and positive `servings`.
Quantities describe the ingredients for those servings. Inventory retains its
existing `days` wire shape, restrictions and confirmed-inventory parser.
Both normalized responses pass the same validator before storage.

The generated-draft store entry point and publication transaction also apply
the common contract. Idempotent generation/publication replays validate the
stored revision rather than returning an old invalid success. The check is
before publication status/version/idempotency writes, so rejection does not
archive the previous published menu or mutate the failed draft.

Editable manual partial drafts may still exist: they are not a successfully
generated or published weekly menu. Historical incomplete published records
remain readable; this task does not rewrite them. Existing Shopping guidance
for legacy missing ingredients remains in force. They cannot be newly
authorized as publication/generation success through replay.

## Bounded internal retries

`MAX_WEEKLY_MENU_CONTRACT_ATTEMPTS = 2` total attempts (one retry) in the common
generation service, only for malformed/invalid provider contract responses.
Each auxiliary attempt keeps `WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY`: one
external request, no provider fallback. No error text or previous raw response
is appended to prompts/logs. Existing request idempotency stays the same.

Exhaustion returns `GENERATOR_VALIDATION_FAILED` to existing safe Telegram
failure handling, without a new successful draft. Transport/provider outages,
authorization failures and storage/conflict failures do not trigger contract
retries. There is no DB transaction across provider requests. This is internal
attempt accounting, not another user action; no quota machinery is introduced.

## Shopping and regression evidence

Ingredient persistence remains `household_weekly_menu_entry_ingredients` via
the existing `WeeklyMenuIngredientInput` mapping and insertion code. The
existing Shopping derivation consumes those rows and scales quantities using
servings. Tests exercise Fast Menu -> persistence -> publication -> derived
Shopping ingredients, invalid-first/valid-second, exhaustion, replay failure,
and inventory regressions without real provider calls.

Legacy read/Shopping fixtures explicitly seed historical published rows rather
than asking the strengthened publication API to create invalid new menus. New
publication tests use complete valid weeks and assert rollback on rejection.
No validator is patched to permit invalid publication.

Production readiness: NOT_APPLICABLE (repository-only Draft PR). Deployment,
data repair, quota, pricing and the remaining MVP tasks need separate scope.
