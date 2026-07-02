# HealBite Weekly Menu, Shopping List, and Household Rollout Plan

Status: design-only proposal

## Scope

This runbook sequences future implementation. Sprint 7.1A itself performs no
build, deploy, restart, production DB write, schema migration, LLM call, or
Telegram send.

## Migration Strategy

### Phase 1: Household Foundation

Additive schema:

- `households`
- `household_members`
- `member_nutrition_targets`
- dietary restrictions
- permission helpers
- one-member bootstrap

Requirements:

- feature disabled by default;
- no current profile/diary/weight/water data moved;
- idempotent bootstrap;
- rehearsal on production-derived DB copy;
- rollback-compatible older image behavior.

### Phase 2: Weekly Menu MVP

Integrate existing `📋 Меню на неделю` route behind feature flag and allowlist.
MVP supports single-user household, one plan, view, one meal replacement,
deterministic nutrition validation, and no diary writes on plan view/generation.

### Phase 3: Shopping List

Integrate existing `🛒 Список покупок` route behind feature flag and allowlist.
MVP supports active list, generated list from ready plan, manual-only fallback,
add/check/exclude, and refresh preserving manual overlay.

### Phase 4: Sub-Profiles

Integrate existing `👨‍👩‍👧 Семья` route behind feature flag and allowlist.
MVP supports dependent profile creation, age band, nutrition target, hard
restrictions, disable profile, and household boundary checks.

### Phase 5: Linked Adult Accounts

Add account linking, adult member permissions, and ownership transfer.

### Phase 6: Family Portion Allocation

Add shared meals with individual allocations and member target comparisons.

### Phase 7: Family Shopping and Limited Beta

Enable multi-member aggregation and controlled beta rollout.

## Sprint Roadmap

### Sprint 7.1B: Household Foundation

Entry criteria: Sprint 7.1A docs merged, current production stable, DB backup
and rehearsal process available.

Exit criteria: additive schema implemented, one-member bootstrap tested,
permission core tested, feature disabled, no current feature regressions.

### Sprint 7.1C: Weekly Menu MVP

Entry criteria: household foundation deployed feature-disabled, one-member
households exist or lazy bootstrap is ready.

Exit criteria: existing weekly menu button opens real module for allowlist,
disabled users see placeholder, generation validates restrictions and nutrition,
ready plan persists atomically, replacement flow tested, no diary auto-write.

### Sprint 7.1D: Shopping List

Entry criteria: ready weekly plan exists in beta environment and canonical food
resolution MVP is available.

Exit criteria: existing shopping button opens list, generated list refresh
works, manual overlay preserved, checked/excluded states persist, privacy
markers safe.

### Sprint 7.1E: Family Profiles

Entry criteria: household permissions are stable and dependent privacy contract
approved.

Exit criteria: existing family button opens household module, dependent profiles
supported, hard restrictions validated, role matrix enforced.

### Sprint 7.1F: Family Portion Allocation

Entry criteria: two-member household test fixtures stable and recipe yield
representation proven.

Exit criteria: shared recipes allocate different portions, daily target
comparison per member, plan validation handles conflicts.

### Sprint 7.1G: Family Shopping and Limited Beta

Entry criteria: multi-member plans stable in internal alpha, privacy and access
audit green.

Exit criteria: shopping aggregation spans household members, beta allowlist
enabled, rollback tested, production smoke passed.

## Feature Flag and Rollout Matrix

| Stage | Weekly menu | Shopping list | Family | Expected response |
| --- | --- | --- | --- | --- |
| Current | disabled | disabled | disabled | `В разработке` |
| Internal alpha | allowlist | disabled | disabled | real weekly only for allowlist |
| Limited beta | allowlist | allowlist | disabled | real enabled routes for beta |
| Family alpha | allowlist | allowlist | allowlist | family route enabled for internal users |
| General availability | enabled | enabled | enabled | real handlers |

Empty allowlist must not imply global rollout.

## Pre-Deploy Checklist for Future Implementation

- exact target SHA verified;
- worktree clean;
- production baseline verified;
- SQLite online backup created;
- migration rehearsal passed on DB copy;
- feature defaults disabled;
- allowlist empty unless rollout playbook says otherwise;
- no raw PII in logs;
- no generic tools in Telegram product lanes;
- Qdrant unchanged unless explicitly required.

## Smoke Checklist for Future Implementation

- main menu labels unchanged;
- placeholder retained for disabled routes;
- weekly route opens only for allowlisted users;
- shopping route opens only for allowlisted users;
- family route opens only for allowlisted users;
- bottom `Меню` returns to keyboard;
- `Назад` works;
- forged callbacks rejected;
- no dangerous tools;
- no Command Approval;
- no raw user content or health data in logs;
- DB integrity ok;
- existing profile, diary, weight, water, and weekly stats still work.

## Compatibility Test Matrix

| Existing feature | Required outcome |
| --- | --- |
| `/profile` | unchanged |
| profile targets | unchanged |
| `/diary` | unchanged |
| photo confirmation | unchanged |
| `/weight` | unchanged |
| weight history | unchanged |
| `/water` | unchanged |
| `/stats 7d` | unchanged |
| dangerous tool gating | unchanged |
| privacy file-log sanitation | unchanged |

## Open Product Decisions

| Decision | Recommendation |
| --- | --- |
| Plan length | default 7 days; support shorter later |
| Meal count | breakfast, lunch, dinner; snacks optional |
| Leftovers | defer to MVP+1 |
| Budget constraints | capture as soft preference later |
| Pantry leftovers | defer; manual shopping overlay first |
| Manual-only shopping list | yes, as fallback |
| KBJU tolerance | define per target as percent band |
| Nutrition database source | deterministic local/canonical source first |
| Children age bands | broad age bands only |
| Linked adult permissions | adult member self-edit plus shared list edit |
| Plan vs actual report | future weekly report extension |

## Production Non-Changes for Sprint 7.1A

```text
production code changed=false
Telegram keyboard changed=false
button labels changed=false
callback data changed=false
runtime handlers changed=false
schema changed=false
migration added=false
LLM runtime changed=false
Docker/Compose changed=false
production DB changed=false
build performed=false
deploy performed=false
restart performed=false
```
