# HealBite Main Menu Feature Integration

Status: design-only proposal

## Verified Current Telegram UI

The current HealBite main menu is implemented in `gateway/platforms/telegram.py`.
The top-level menu uses `ReplyKeyboardMarkup` and text routes, not inline
callback data. The bottom `Меню` command returns the user to the same reply
keyboard through `_send_healbite_menu_message`.

## Current UI Integration Map

| Button | Current handler or route | Current status | Future module | Scope |
| --- | --- | --- | --- | --- |
| 👤 Мой профиль | `/profile` via `_maybe_handle_healbite_profile_command` | working | member profile / nutrition target | member-scoped |
| 🍎 Дневник еды | `/diary` via `_maybe_handle_nutrition_diary_command` | working | actual meal history | member-scoped |
| 📋 Меню на неделю | `__placeholder__:weekly_menu` via `_dispatch_healbite_keyboard_action` | placeholder | weekly plan | household-scoped |
| 🛒 Список покупок | `__placeholder__:shopping_list` via `_dispatch_healbite_keyboard_action` | placeholder | shopping list | household-scoped |
| ⚖️ Трекер веса | `/weight` via `_maybe_handle_healbite_weight_command`; inline callbacks `weight:*` | working | body metrics | member-scoped |
| 💧 Трекер воды | `/water` via `_maybe_handle_healbite_water_command`; inline callbacks `water:*` | working | hydration tracking | member-scoped |
| 👨‍👩‍👧 Семья | `__placeholder__:family` via `_dispatch_healbite_keyboard_action` | placeholder | household management | household-scoped |
| 📈 Отчет за неделю | `/stats 7d` via `_maybe_handle_nutrition_diary_command` | working | existing weekly report | member-scoped initially |
| ⚙️ Ограничения | `__placeholder__:restrictions` | placeholder | dietary restrictions | member or household-scoped by future decision |
| ❓ Помощь | `__placeholder__:help` | placeholder | help | product-scoped |

Current placeholder response:

```text
В разработке
```

## UI Compatibility Contract

Existing top-level labels, emoji, button order, two-column layout, and bottom
`Меню` behavior must remain unchanged. Sprint 7.1 feature work plugs into the
existing entries instead of adding parallel top-level buttons.

Do not add duplicate top-level buttons such as `Рацион`, `План питания`,
`Продукты`, `Домохозяйство`, or `Члены семьи`.

## Placeholder Lifecycle

Before feature enablement:

- button remains visible;
- handler answers `В разработке`;
- no DB writes;
- no LLM calls;
- no generic Hermes dispatch;
- route marker remains privacy-safe.

After implementation and feature enablement:

- the same route opens the real section;
- non-allowlisted users still receive the safe placeholder;
- empty allowlist does not mean global rollout;
- no second parallel handler is introduced for the same button.

## Back Navigation Contract

Every future screen must support `Назад`, `В главное меню`, and `Отмена`.
The bottom `Меню` entry must continue to return to the main keyboard.

## Callback Compatibility

Top-level weekly menu, shopping list, and family entries are text routes today.
Future inline callbacks inside these sections must use stable enum-like callback
prefixes that do not contain user IDs, household IDs, member names, recipe
names, meal titles, shopping item names, or raw serialized payloads.

Existing callback prefixes that must remain compatible:

- `weight:*`
- `water:*`

Future recommended prefixes:

- `weekly:*`
- `shop:*`
- `household:*`

## Future `📋 Меню на неделю` Screen

Recommended actions:

- current week;
- next week;
- create menu;
- open ready menu;
- replace meal;
- refresh one day;
- plan archive;
- back.

MVP scope:

- create one plan;
- view plan;
- replace one meal.

Opening the button must not create a new plan. If a ready plan already exists,
the screen should show it first.

## Future `🛒 Список покупок` Screen

Recommended actions:

- open active list;
- refresh from menu;
- add item;
- check item;
- exclude item;
- clear checked state;
- back.

Recommendation: if no ready weekly plan exists, open a manual-only shopping
list with a visible option to create a weekly menu. This keeps the shopping
entry useful without forcing LLM generation. Refresh from a plan remains a
separate explicit operation.

## Future `👨‍👩‍👧 Семья` Screen

Recommended actions:

- household members;
- add profile;
- edit profile;
- nutrition target;
- dietary restrictions;
- permissions;
- link Telegram account;
- disable profile;
- back.

Household is not a Telegram group chat, not a bot admin list, and not Telegram
contacts.

## Integration With Existing Working Sections

### Profile

Existing `/profile` remains the primary member profile. Household bootstrap
creates a primary household member linked to that profile. Weekly plan
generation reads versioned nutrition target snapshots from the member profile.

### Food Diary

The diary remains actual consumption. Weekly menu is planned consumption.
Planned meals never write to `nutrition_log` unless the user explicitly chooses
a future `Добавить в дневник` action.

### Weight

Weight history stays member-scoped. Household-level weight aggregation is out of
scope.

### Water

Water tracking stays member-scoped. Household-level water totals are out of
scope.

### Weekly Report

The current `/stats 7d` report remains member-scoped. A future report may add
plan-vs-actual comparison using planned nutrition summaries and consumed diary
summaries.

## Rollout State Matrix

| Stage | Button visible | Response |
| --- | ---: | --- |
| Current | yes | `В разработке` |
| Internal alpha | yes | real handler only for allowlist |
| Limited beta | yes | real handler for beta allowlist |
| Disabled for user | yes | safe placeholder |
| General availability | yes | real handler |

## UI Routing Test Matrix

| Scenario | Expected result |
| --- | --- |
| Existing labels render | labels unchanged |
| Existing layout renders | two-column order unchanged |
| Placeholder disabled route | `В разработке`, no DB write, no LLM |
| Feature-enabled weekly route | opens weekly menu module |
| Feature-enabled shopping route | opens shopping module |
| Feature-enabled family route | opens household module |
| Bottom `Меню` | returns to main keyboard |
| `Назад` in nested screen | returns to prior screen |
| Forged callback | rejected with safe local response |
| Non-allowlisted feature route | safe placeholder, no generic dispatch |
| Multiline keyboard input | local rejection |

## Explicit Exclusions

```text
no Telegram keyboard changes
no callback changes
no runtime code
no schema migration
no feature implementation
no LLM calls
no production DB writes
no build
no deploy
no restart
```
## Actual Button Layout Contract

The verified current layout has five rows and two columns:

```text
[ 👤 Мой профиль ] [ 🍎 Дневник еды ]
[ 📋 Меню на неделю ] [ 🛒 Список покупок ]
[ ⚖️ Трекер веса ] [ 💧 Трекер воды ]
[ 👨‍👩‍👧 Семья ] [ 📈 Отчет за неделю ]
[ ⚙️ Ограничения ] [ ❓ Помощь ]
```

Sprint 7.1A focuses on weekly menu, shopping list, and family. The existing
restrictions and help placeholders are documented as current UI facts but are
not part of the Sprint 7.1A implementation roadmap.
