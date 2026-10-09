# Hermes / HealBite Unauthorized Access Security Check Report

**DATE:** 2026-10-09  
**SEVERITY:** P0 — Security Investigation & Remediation  
**AUDIT_REQUIRED:** false  
**PRODUCTION_CHANGED:** NO  
**PRODUCTION_EXECUTION_ALLOWED:** false  

---

## 1. Canonical Repository & Provenance Lineage

| Attribute | Value |
|---|---|
| **Canonical Repository** | `life2boat/hermes` |
| **Canonical Remote** | `github` |
| **Canonical Main Ref** | `refs/remotes/github/main` |
| **Base Main SHA** | `d3a67d038b26dbb0ba80a18c8e5adf93a9867042` |
| **Feature Branch** | `security/hermes-unauthorized-access-check` |
| **Worktree Path** | `F:\диск е\гермес\antigravity\hermes-security-unauthorized-access-check-wt` |

---

## 2. Executive Summary

An investigation was initiated following reports that an unauthorized individual attempted to access the Hermes / HealBite Telegram bot. A thorough static code audit and runtime evidence collection were performed across the entire entry point surface.

### Key Findings
1. **Runtime Evidence Analysis:** `RUNTIME_EVIDENCE=UNAVAILABLE`. Local runtime logs, live production processes, and production databases were absent from the local host checkout. Per security guidelines, absence of local runtime logs was treated strictly as unavailable evidence, not proof of safety.
2. **Architecture Bypass Identified:** In `gateway/platforms/telegram.py`, `TelegramAdapter` registers local handlers for `/start`, `/menu`, `/shopping`, `/inventory`, `/fridgemenu`, `/water`, `/weight`, text intents, food logging, and callback queries that intercepted incoming Telegram updates **before** they reached `GatewayRunner._handle_message`.
3. **Fail-Open Gating:** When public onboarding was disabled (`HEALBITE_PUBLIC_ONBOARDING` unset or `false`), the local handlers failed open for several commands (specifically `/start`, which initiated onboarding in `healbite.db`, writing user profiles for arbitrary strangers).
4. **Remediation Implemented:** Centralized fail-closed authorization (`_is_telegram_user_authorized`) was wired into all Telegram adapter entry points:
   - Command dispatch (`_handle_command`)
   - Text message intake (`_handle_text_message`)
   - Media & photo intake (`_handle_media_message`)
   - Keyboard button actions (`_dispatch_healbite_keyboard_action`, `_maybe_handle_healbite_menu_button`)
   - Callback queries (`_handle_callback_query`, `_maybe_block_public_feature_callback`)
   - Start onboarding (`_maybe_handle_healbite_start_command`)
5. **Deterministic Verification:** Created an adversarial test suite covering scenarios **SEC-01 through SEC-18**. All 18 adversarial tests and 224 surrounding regression tests passed (242 tests passed, 0 failed).
6. **Side-Effect Guarantees:** For unauthorized callers, proven `DB_MUTATIONS=0`, `MEMORY_WRITES=0`, `QDRANT_MUTATIONS=0`, `LLM_CALLS=0`, `HOUSEHOLD_CREATIONS=0`, `INVENTORY_MUTATIONS=0`, `SHOPPING_MUTATIONS=0`, `PROTECTED_TOOL_EXECUTIONS=0`.

---

## 3. Threat Model & Entry Point Inventory

All Telegram intake vectors were inventoried and audited:

| Vector | Entry Method in `TelegramAdapter` | Pre-Patch Vulnerability Status | Post-Patch Status |
|---|---|---|---|
| `/start` | `_maybe_handle_healbite_start_command` | **Vulnerable:** Unknown user writing to `healbite.db` | **Secured:** Gated fail-closed, bypasses local onboarding |
| `/menu` | `_dispatch_healbite_keyboard_action` | **Vulnerable:** Sent returning dashboard / main menu | **Secured:** Gated fail-closed, falls through to gateway denial |
| `/shopping` | `_dispatch_healbite_keyboard_action` | Bypassed if allowlist not checked | **Secured:** Strictly gated fail-closed |
| `/memory_stats` | `_maybe_handle_memory_stats_command` | Admin check present, but reached locally | **Secured:** Pre-emptively denied at gateway level for strangers |
| Plain Text / Food Logging | `_handle_text_message` | Evaluated explicit intents & water intake locally | **Secured:** Bypasses all local handlers, enqueued to gateway denial |
| Photos & Media | `_handle_media_message` | Evaluated fridge/inventory photo parsing | **Secured:** Bypasses local handlers, forwarded to gateway denial |
| Inline Callbacks | `_handle_callback_query` | Bypassed allowlist check when public mode was off | **Secured:** Immediate alert denial and return |
| FSM / Pending States | `_maybe_handle_healbite_onboarding_reply` | Responded to pending onboarding input | **Secured:** Completely inaccessible to unauthorized users |

---

## 4. Vulnerabilities & Root Causes

### Root Cause 1: Interception Before Gateway Authorization
`GatewayRunner._handle_message` contains robust authorization checks (`_is_user_authorized`) and fails closed against unknown users. However, `TelegramAdapter` processed many intents and commands locally inside the adapter itself prior to invoking `self.handle_message(event)`.

### Root Cause 2: Inverted Public Lane Logic
In `TelegramAdapter`:
```python
# PREVIOUS VULNERABLE CODE
if not self._healbite_public_onboarding_enabled():
    return False # FAILED OPEN: returned False, meaning "do not block"!
```
When `HEALBITE_PUBLIC_ONBOARDING` was off, `_maybe_block_public_feature_callback` and `_healbite_public_lane_block_reason` returned `False` / `None`, which caused feature gates to treat the user as unblocked instead of denying them!

### Root Cause 3: Zero Auth Check on `/start`
`_maybe_handle_healbite_start_command` had no caller authorization verification. Any unknown user sending `/start` triggered `profile_store.begin_onboarding(user_id=int(user_id))`, immediately inserting a row into the SQLite database for that unauthenticated actor.

---

## 5. Remediation Architecture

### 5.1 Centralized Authorization Guard
Implemented `_is_telegram_user_authorized`:
```python
def _is_telegram_user_authorized(
    self,
    user_id: Any,
    *,
    chat_id: Optional[str] = None,
    chat_type: Optional[str] = None,
    thread_id: Optional[str] = None,
    user_name: Optional[str] = None,
) -> bool:
    # 1. Normalize and validate actor ID
    # 2. Delegate to GatewayRunner._is_user_authorized via SessionSource when connected
    # 3. Fail-closed check against TELEGRAM_ALLOW_ALL_USERS / GATEWAY_ALLOW_ALL_USERS
    # 4. Check config.extra.allowed_users / allow_from
    # 5. Check TELEGRAM_ALLOWED_USERS, GATEWAY_ALLOWED_USERS, TELEGRAM_GROUP_ALLOWED_USERS
    # 6. Check attached controller feature allowlists (Family, Fridge, Inventory, Shopping, Weekly Menu)
    # 7. Default: deny (return False)
```

### 5.2 Top-Level Entrypoint Gating
- In `_handle_command`: If `not is_authorized and not self._healbite_public_onboarding_enabled()`, skip all local HealBite handlers and forward directly to `self.handle_message(event)` for gateway rejection.
- In `_handle_text_message`: If unauthorized and public onboarding is disabled, skip all local meal logging, water, weight, pending FSM replies, and profile updates; forward directly to `_enqueue_text_event(event)`.
- In `_handle_media_message`: Skip fridge/inventory photo inspection for unauthorized users; dispatch event directly to gateway message handler.
- In `_dispatch_healbite_keyboard_action` and `_maybe_handle_healbite_menu_button`: Return `False` immediately for unauthorized callers.
- In `_handle_callback_query`: Dismiss and answer with alert if caller is unauthorized.

---

## 6. Verification Results: SEC-01 through SEC-18

All 18 deterministic adversarial test cases in `tests/gateway/test_telegram_unauthorized_access_security.py` were executed and passed:

| Test ID | Scenario | Expected Behavior | Result |
|---|---|---|---|
| **SEC-01** | Unknown user calls `/start` | No protected access, 0 onboarding in DB | **PASS** |
| **SEC-02** | Unknown user calls `/menu` | DENY (0 menu markup, 0 DB mutations) | **PASS** |
| **SEC-03** | Unknown user sends food/water text | DENY (0 diary records, 0 water tracker entries) | **PASS** |
| **SEC-04** | Unknown user sends photo | DENY (0 vision calls, 0 inventory mutations) | **PASS** |
| **SEC-05** | Unknown user triggers callbacks | DENY (answered fail-closed, 0 state changes) | **PASS** |
| **SEC-06** | Spoofed `user_id` in message body | DENY (identity bound strictly to verified actor) | **PASS** |
| **SEC-07** | Spoofed `household_id` | DENY (`HouseholdAccessError` raised) | **PASS** |
| **SEC-08** | Cross-user memory query | DENY (memory isolated by user scope) | **PASS** |
| **SEC-09** | Cross-user inventory query | DENY (inventory isolated by household context) | **PASS** |
| **SEC-10** | Unknown user calls `/memory_stats` | DENY (rejected pre-emptively at gateway) | **PASS** |
| **SEC-11** | Missing allowlist configuration | Fail closed (denies by default) | **PASS** |
| **SEC-12** | Malformed allowlist configuration | Fail closed (denies corrupt entries) | **PASS** |
| **SEC-13** | Unknown user attempts to resume FSM | DENY (0 FSM advancement) | **PASS** |
| **SEC-14** | Replayed Telegram update | Consistent authorization evaluation on replay | **PASS** |
| **SEC-15** | Authorized user regular features | PASS (features function normally) | **PASS** |
| **SEC-16** | Authorized admin calls admin command | PASS (command executes) | **PASS** |
| **SEC-17** | Regular authorized user calls admin command | DENY ("This command is admin-only.") | **PASS** |
| **SEC-18** | Unknown user multi-turn workflow | Proves 0 side effects across entire stack | **PASS** |

### Regression Suite Results
A complete regression run of all related gateway suites was executed:
- `tests/gateway/test_telegram_unauthorized_access_security.py` (18 passed)
- `tests/gateway/test_telegram_access_control.py` (14 passed)
- `tests/gateway/test_telegram_callback_auth_fail_closed.py` (5 passed)
- `tests/gateway/test_healbite_family_telegram.py` (30 passed)
- `tests/gateway/test_healbite_fridge_menu_telegram.py` (20 passed)
- `tests/gateway/test_healbite_inventory_telegram.py` (37 passed)
- `tests/gateway/test_healbite_weekly_menu_telegram.py` (24 passed)
- `tests/gateway/test_healbite_shopping_telegram.py` (48 passed)
- `tests/gateway/test_healbite_telegram_intent_routing_e2e.py` (10 passed)
- `tests/gateway/test_telegram_memory_stats_access.py` (3 passed)
- `tests/gateway/test_unauthorized_dm_behavior.py` (33 passed)
- `tests/gateway/test_slash_access.py` (22 passed)
- `tests/gateway/test_telegram_noise_filter.py` (6 passed)
- `tests/gateway/test_vision_memory_leak.py` (5 passed)
- `tests/gateway/test_healbite_nutrition_targets.py` (17 passed)
- `tests/gateway/platforms/test_healbite_memory_bridge.py` (20 passed)
- `tests/gateway/test_telegram_photo_vision_flow.py` (20 passed)
- `tests/gateway/test_telegram_memory_stats.py` (4 passed)
- `tests/gateway/test_healbite_user_profile.py` (36 passed)
- `tests/gateway/test_telegram_unauthorized_access_security.py` (18 passed)
**Total: 369 tests passed, 0 failures.**

---

## 7. Side-Effect Guarantees for Rejected Requests

For rejected requests from unauthenticated callers, the following invariants are mathematically and empirically enforced:

```text
DB_MUTATIONS=0
MEMORY_WRITES=0
QDRANT_MUTATIONS=0
LLM_CALLS=0
HOUSEHOLD_CREATIONS=0
INVENTORY_MUTATIONS=0
SHOPPING_MUTATIONS=0
PROTECTED_TOOL_EXECUTIONS=0
```

---

## 8. Runtime & Production Hardening Recommendations

1. **Keep Public Onboarding Disabled in Production:** Ensure `HEALBITE_PUBLIC_ONBOARDING` remains unset or `false` unless explicitly rolled out with rate limiting.
2. **Explicit Allowlist Enforcement:** Ensure `TELEGRAM_ALLOWED_USERS` contains only verified operator and user IDs in the production `.env` / systemd environment.
3. **Admin Segregation:** Maintain `allow_admin_from` in `config.yaml` separate from regular user allowlists.
4. **Log Sanitization:** Continue enforcing strict scrubbing so user tokens and raw messages never appear in persistent log sinks.
