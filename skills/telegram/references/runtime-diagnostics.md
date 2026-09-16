# Telegram runtime diagnostics

Confirm the intended container/gateway, image/revision when available, restart
count, profile, and transport mode before diagnosis. Long polling expects one
owner; webhook mode also requires the intended HTTPS route and secret.

Use sanitized logs to classify startup, network, Telegram API, formatting,
timeout, database-lock, provider, and traceback failures. Verify token and
allowlist presence without values. For routing failures, inspect both gateway
message guards: commands that must work while an agent is blocked must bypass both message guards.

Reproduce offline with mocked API calls and synthetic updates. Preserve session,
callback, user/chat, and topic isolation; implement the smallest repair and run
focused Telegram plus adjacent gateway regressions before any authorized rollout.
