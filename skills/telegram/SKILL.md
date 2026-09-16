---
name: telegram
description: Use for Hermes Telegram runtime and routing diagnostics.
---

# Telegram runtime router

Use this skill for Telegram delivery, polling/webhook, authorization, routing,
callback/FSM, formatting, connectivity, or no-send smoke work. Do not use it
for a general deploy, Memory OS repair, BotFather token rotation, or a live user
send unless the task explicitly authorizes that action.

## When to Use

Use only for Telegram-specific runtime or routing work. Select the smallest
relevant reference set below.

## Select references

| Task | Read |
| --- | --- |
| Runtime offline, polling conflict, webhook, restart, or transport diagnosis | [runtime diagnostics](references/runtime-diagnostics.md) |
| Token handling, authorization, user/chat isolation, duplicate polling, or no-send evidence | [safety invariants](references/safety-invariants.md) |

## Procedure

Start with sanitized, read-only identity and status inspection:

```bash
./scripts/healbite status
./scripts/healbite logs --last 80
```

Trace the smallest failure through the adapter, base-session queue,
`gateway/run.py` guards, handler/callback dispatch, and focused synthetic tests.
Use `scripts/run_tests.sh` for the relevant test path. Do not load deployment or
memory procedures merely because Telegram is part of the application.

## Failure/Rollback

Stop when runtime identity, token ownership, mode, or configuration authority
is ambiguous. Never start a second polling process, print token/config/log
content, mutate an allowlist/webhook, restart, deploy, or send a live message
without explicit approval.

## Completion

Report safe classifications only: runtime identity, mode, ownership state,
restart count, focused-test outcome, and the next authorized action.
