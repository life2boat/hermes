# Sprint 7.0C2 - Weekly Weight Reminder Delivery

## Status
Not implemented in Sprint 7.0C1.

Sprint 7.0C1 delivers:
- weight entry capture;
- append-only weight history;
- profile weight synchronization;
- macro target recalculation;
- Telegram weight UX without generic-agent routing.

## Why split
Reminder storage and due/dedupe helpers exist, but Sprint 7.0C1 does not register a runtime scheduler task, does not run a reminder tick, and does not deliver Telegram reminder messages. User-visible reminder controls must stay hidden until delivery exists.

## Required runtime work
- register a single process-level reminder scheduler task;
- poll eligible users in batches;
- apply timezone-aware weekday/time matching;
- deliver via the safe Telegram adapter path;
- update last-sent atomically;
- prevent duplicates across restarts and repeated ticks;
- isolate delivery failures per user;
- cancel cleanly on shutdown.

## Required tests
- scheduler registration and shutdown cancellation;
- eligible-user query and disabled-user skip;
- single-send dedupe across repeated ticks;
- restart-safe dedupe;
- delivery failure isolation;
- timezone fallback behavior;
- privacy/file-log coverage with no recipient or reminder-setting leakage.
