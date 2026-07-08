# CURRENT_STATE changelog

## 1.2.0 — 2026-07-08

Added:
- current-main Qwen vision integration state;
- source and integration implementation commit identifiers;
- task-scoped `QWEN_API_KEY` routing status;
- local validation results for focused, related and agent-check suites.

Changed:
- V2-R1 status moved from in-progress to implemented and locally validated;
- CURRENT_STATE verification base moved to project main `60f84093c0fe82d29814c2ac8e3c0fb6dc847e7b`;
- next allowed V2-R1 sequence now starts with Draft PR creation and CI.

Safety:
- Qwen remains not deployed and not active in production;
- production build/deploy/restart not performed;
- provider requests and Telegram smoke not performed;
- DB/Qdrant unchanged;
- no secrets, private IDs or raw provider responses stored.

## 1.1.0 — 2026-07-08

Added:
- completed Gemini reason probe;
- Gemini Developer API/auth confirmation;
- ListModels 403 result;
- active V2-R1 Qwen worktree;
- call_policy defect;
- separation of implementation/merge/deploy/smoke.

Changed:
- old V2 base mismatch moved to history;
- Gemini next step changed to Google Console audit;
- Qwen explicitly marked not deployed.
- clarified that the recorded SHA is the state-verification base, not a self-referential future main HEAD.

Safety:
- production unchanged;
- build/deploy not performed;
- DB/Qdrant unchanged;
- no secrets stored.

## 1.0.0 — 2026-07-08

- initial source-of-truth file created.
