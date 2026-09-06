# Conversational updates to the existing HealBite profile

Repository candidate based on `ea423c750720248dc3e1e082cf02f61d165a62f5`.
No deployment or production migration is authorized by this document.

## Storage and compatibility

`HealBiteUserProfileStore` remains the authority; the table is `profiles`.
The existing additive `_PROFILE_COLUMNS` initializer gains nullable
`household_size INTEGER`, `cooking_frequency TEXT`, and `budget TEXT`.
Old rows and old-schema read-only loads retain unspecified values. The existing
profile initializer upgrades a local schema idempotently. It is not a substitute
for an authorized staged production migration; production qualification remains
a separate task. Nutrition onboarding, calculations and required fields do not
gain new prerequisites.

`effective_household_size(profile)` returns an explicit positive integer or 1.
The fallback is never persisted. Cooking frequency and budget are bounded,
whitespace-normalized user text (up to 200 characters); no currency, period,
location, prices or menu behavior is inferred. Household size accepts 1–100.
The report displays Russian labels. Budget is absent from the weekly menu
snapshot and from Memory sync.

## Extraction and merge

`extract_profile_delta` recognizes complete anchored Russian declarations:
“Нас дома трое”, “У меня аллергия на арахис”, “Не люблю рыбу и печень”,
“Люблю итальянскую и азиатскую кухню”, “Готовлю примерно 4 раза в неделю”,
“Бюджет на питание 20000 рублей”. Comma/semicolon/“и” joins before another
recognized clause permit atomic multi-field updates. Unsupported clauses and
questions are no-ops; this bounded MVP is not a general natural-language parser.
No provider is involved.

`validate_profile_delta` rejects unknown fields, nulls, wrong types, invalid
sizes and malformed text before persistence. Omitted keys leave the corresponding
columns untouched. Preference lists append with exact case-insensitive duplicate
suppression, preserving existing legacy text as a prefix. Explicit empty lists
clear a field; the grammar requires “очисти аллергии”, “очисти нелюбимые продукты”
or “очисти предпочтения”. Null is not a clear operation.

Aliases map `disliked_foods` to `stop_products` and `preferred_cuisines` to
`preferences`. A `BEGIN IMMEDIATE` transaction reads the current owner row and
writes only the validated fields. Existing users with no `profiles` row can
create that row in the same transaction. Unknown owners are rejected.

## Routing and Memory boundary

Telegram text routing calls the profile handler after existing onboarding,
inventory, weight and water pending handlers, before generic dispatch. It accepts
only private sender-owned chats authorized by the existing runner authorization
path. Existing onboarding retains priority; this handler does not expand access.

After commit, only changed allergies/dislikes/cuisines go to the existing
`HealBiteConversationalMemoryBridge.sync_profile_preferences`. Existing enabled
and user gates remain authoritative. Its canonical `HealBiteMemoryBridge.upsert_fact`
uses stable `profile_<field>` keys and the normal revisioned SQLite/outbox path.
It does not alter fact UUIDs, epochs, vector IDs or schemas. Explicit clears
replace the corresponding profile snapshot with an explicit cleared-field marker.
It does not erase independently captured historical conversational facts.

Memory sync is optional best effort after profile commit: disabled/unavailable
Memory or sync failure cannot roll back or erase the profile. No durable profile
sync retry queue is introduced. A later explicit update can retry the snapshot;
this MVP does not claim transactional convergence across both stores. Logs contain
only fixed outcomes, not user text or profile values.

## Review and release boundary

AI review: existing store, explicit grammar, atomic deltas, scoped transport,
additive legacy initialization and focused negative/isolation tests.
No new provider prompt, parallel profile subsystem or architectural service is
introduced; a design contract suffices without a new ADR.
Production readiness is NOT_PERFORMED: no image, live migration, runtime change,
Qdrant mutation, Telegram smoke, access change or provider call is part of this PR.
