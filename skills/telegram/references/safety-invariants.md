# Telegram safety invariants

Bind the bot token to the intended gateway/profile, then authorize each update
against private-user, group-user, group-chat, and feature scope before routing
or reading FSM state. Use synthetic cross-user/cross-chat routing and FSM tests;
never report identifiers.

Treat the Telegram bot token as a bearer credential. Preventing the value from
entering output is stronger than redaction: never print it, pass it on a command
line, or retain it in evidence. Reports must contain neither the token nor
credential-bearing URLs.

Permit exactly one active `getUpdates` long-polling consumer (long polling). Two runtimes using
the same bot token must not be active because Telegram terminates competing
`getUpdates` sessions with a conflict. Never start a second polling process to
diagnose a live token.

Default health checks to no-send operations. Treat `getMe` as evidence of
credential/network/API readiness, not as proof of delivery; require zero
send/edit API calls and zero outbound messages unless a live smoke is separately
authorized.
