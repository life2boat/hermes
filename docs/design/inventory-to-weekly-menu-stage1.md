# Inventory To Weekly Menu: Stage 1

Stage 1 introduces a feature-disabled, household-safe domain foundation for
turning an explicitly confirmed product snapshot into a hidden weekly-menu
draft and a locally calculated shopping delta.

## Lifecycle

1. Text input creates a `pending` inventory snapshot. Photo recognition may
   supply only pending candidate items; it never saves an image, invokes a
   fallback, confirms a snapshot, or generates a menu by itself.
2. A snapshot can be edited only while pending. Confirmation is idempotent;
   cancelled and pending snapshots are rejected for menu generation.
3. Inventory generation resolves the caller's existing household and accepts
   only its confirmed snapshot. It creates a draft revision only and never
   publishes it.
4. The locally calculated remainder can be converted to existing
   `GeneratedShoppingItemInput` values after its menu-entry provenance is
   available. Stage 1 does not mutate a Shopping List automatically.

## Safety Contract

The new `HEALBITE_INVENTORY_TEXT`, `HEALBITE_INVENTORY_PHOTO`, and
`HEALBITE_WEEKLY_MENU_INVENTORY` gates default to disabled and use the shared
allowlist parser. The photo adapter accepts only already-resolved candidates;
it does not contain provider routing or credential logic.

Inventory menu requests use exactly seven days and three meals per day. The
provider response is parsed as a JSON object with a single `days` field;
text outside JSON and any unknown fields are rejected. Ingredient quantities
are decimal strings with known compatible units. Allergies and restrictions
from the existing profile snapshot are hard exclusions.

The shopping delta is computed locally by `(normalized_name, unit)`. Only
known compatible inventory quantities are subtracted. Unknown quantities and
incompatible units never make an ingredient fully available, and no LLM
shopping-delta field is trusted.
