---
name: telegram
description: Use for Hermes Telegram runtime troubleshooting.
---

# Telegram Runtime Debug Skill

Use this skill to diagnose Hermes/HealBite Telegram delivery, authorization, routing, polling, webhook, formatting, or FSM failures. Start read-only, sanitize all evidence, and restart or reconfigure the gateway only after the cause is identified and the operator authorizes the mutation.

## When to Use

- The bot is offline, silent, looping, returning unauthorized, or failing in groups/topics.
- Telegram polling reports a conflict, webhook delivery stops, or the gateway repeatedly restarts.
- A specific command, callback, FSM transition, photo flow, or reply format fails.
- Do not use this skill for BotFather token rotation, production deploy, or sending a live user smoke unless separately authorized.

## Prerequisites

- Read `AGENTS.md`, `docs/CURRENT_STATE.md`, `gateway/platforms/telegram.py`, and the [Telegram user guide](../../website/docs/user-guide/messaging/telegram.md).
- Use `terminal` for safe diagnostics, `read_file` for adapter/config contracts, and `search_files` for handlers and tests.
- Know whether the runtime uses long polling or webhook mode and whether it is a profile-specific gateway.
- Never print bot tokens, allowed-user values, chat/user IDs, personal messages, raw production logs, or correlation identifiers.
- Obtain explicit approval before token/config changes, container recreation, restart, deploy, webhook mutation, or a live Telegram send.

## How to Run

Capture a fresh sanitized baseline from the project root:

```bash
./scripts/healbite status
./scripts/healbite logs --last 80
```

Use focused tests through `scripts/run_tests.sh`; do not invoke the test runner directly. Prefer an offline synthetic event or temporary database over a live Telegram message.

## Quick Reference

| Symptom | First gate |
| --- | --- |
| Bot offline | process/container state and restart count |
| Polling conflict | duplicate live gateway using the same token |
| Webhook silent | mode, public HTTPS route, secret presence, local port |
| Unauthorized | allowlist presence and correct user/group scope |
| Works in DM only | BotFather privacy, membership refresh, mention filters |
| Command/callback ignored | both gateway guards, routing, callback data, FSM state |
| Bad formatting | parse mode, escaping, message length, edit fallback |

One live gateway/profile must own one Telegram bot token. Never start a second polling process to test a token already in use.

## Procedure

1. **Define the smallest failing path.** Record platform mode, DM/group/topic context, update type, handler/callback name, expected state transition, and safe error class. Do not copy message content or identifiers.
2. **Confirm runtime identity.** Run `./scripts/healbite status`; verify the intended `hermes-bot` or gateway instance, image/revision when available, container status, restart count, config source classification, and database path. Do not infer production identity from a checkout alone.
3. **Check ownership and duplicates.** Confirm only one live polling/webhook gateway uses the token. Inspect profile-scoped token locks and process/container inventory. A Telegram `getUpdates` conflict is evidence of duplicate ownership, not a reason to start another poller.
4. **Determine transport.** If `TELEGRAM_WEBHOOK_URL` is absent, expect long polling. If present, require the webhook secret, public HTTPS reachability, correct reverse-proxy route, and local port. Do not switch modes during diagnosis.
5. **Inspect sanitized logs.** Use `./scripts/healbite logs --last 80`, which filters sensitive content. Classify startup, network/DNS/proxy, Telegram API, parse-mode, timeout, database-lock, provider, and traceback errors without pasting raw production logs.
6. **Check authorization gates.** Verify required token and allowlist variables are present without values. Distinguish private-user allowlists, group-user allowlists, and group-chat allowlists. Confirm fail-closed behavior when no user is authorized.
7. **Check Telegram delivery gates.** For groups, verify BotFather privacy/admin delivery, rejoin requirements after privacy changes, mention requirements, ignored topics, and exclusive multi-bot mentions. Negative group IDs are normal but must never be reported.
8. **Trace routing in code.** Follow update parsing into `gateway/platforms/telegram.py`, base-adapter active-session queuing, `gateway/run.py` control-command interception, command resolution, callback dispatch, and FSM state. Commands that must work while an agent is blocked must bypass both message guards.
9. **Inspect the feature state safely.** Use a temporary DB or read-only status path. Verify expected FSM keys, expiry/cancel behavior, callback payload validation, and user/chat isolation. Do not repair production rows during diagnosis.
10. **Reproduce offline.** Run the narrow Telegram test file through `scripts/run_tests.sh`, then adjacent gateway regressions. Use mocked Bot API calls and synthetic events; assert transitions, escaping, chunking, authorization, and no-send behavior.
11. **Implement the smallest repair.** Keep provider/auth exceptions masked, preserve prompt caching and message alternation, and avoid unrelated adapter refactors. Add regression coverage for the exact routing or state invariant.
12. **Validate before rollout.** Run focused tests, `bash scripts/agent_check.sh`, `git diff --check`, and repository-required full tests. A code fix or green CI does not authorize production deploy or restart.
13. **Roll out only when requested.** Use the production deployment skill for an authorized image rollout. For an approved config-only restart, capture baseline and rollback values without printing them, change only the named setting, and verify ownership before restarting.
14. **Verify post-state.** Require running status, `restart_count=0`, expected polling/webhook mode, no duplicate owner, no traceback or token leakage, and offline/synthetic handler success. Perform a live user smoke only when the task explicitly authorizes it.

## Failure/Rollback

- Stop when runtime identity, token ownership, mode, or config authority is ambiguous.
- Do not rotate a token, delete a webhook, alter allowlists, or restart repeatedly to discover the cause.
- Revert only the explicitly changed config/code artifact through its approved rollback path. Preserve sanitized before/after classifications.
- If a rollout fails, use the production deployment rollback procedure; do not manually recreate adjacent services or modify SQLite/Qdrant.

## Pitfalls

- Testing by launching a second polling gateway with the production token.
- Reading raw logs or environment dumps instead of filtered presence/classification output.
- Treating BotFather privacy, Hermes authorization, and mention routing as one gate.
- Fixing only `gateway/run.py` while the base adapter still queues the control message.
- Sending Markdown/HTML without the adapter's required escaping or exceeding Telegram length limits.
- Using a live message, DB write, restart, or webhook change when an offline synthetic reproduction is available.

## Verification

- [ ] Runtime instance, profile, transport mode, and token ownership were identified safely.
- [ ] No duplicate poller/webhook owner is active for the token.
- [ ] Authorization, group delivery, mention/topic, both message guards, routing, and FSM gates were checked in order.
- [ ] Focused tests reproduce the failure and pass after the repair.
- [ ] `scripts/agent_check.sh`, `git diff --check`, and required full tests passed before rollout.
- [ ] Logs and reports contain no secrets, identifiers, messages, or raw correlation data.
- [ ] No restart, deploy, config change, DB write, or live Telegram send occurred without explicit approval.
