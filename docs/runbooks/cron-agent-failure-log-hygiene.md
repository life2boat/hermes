# Cron Agent Failure Log Hygiene

This note documents the D5C0-F2 scheduler change.

- Contained cron-agent execution failures raised at the agent result boundary are logged without a Python traceback.
- Unexpected scheduler/runtime exceptions still emit a traceback via the generic exception handler.
- The failed-job return contract, job-state semantics, and retry behavior are unchanged.
- The fix does not change weekly menu runtime, shopping runtime, or provider retry/fallback behavior.
- Production does not include this change until a later build/deploy stage.
- Sprint 7.1D5C1 remains blocked until this merged fix is deployed and a clean observation window is completed.
