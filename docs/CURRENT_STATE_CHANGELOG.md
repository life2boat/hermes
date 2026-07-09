# CURRENT_STATE changelog

## 1.2.4 - 2026-07-09

Added:
- local component-confirmation flow for mixed-plate meal photos;
- explicit correction commands for component replacement, addition, removal and weight confirmation;
- focused regression coverage for Stage-1 inventory confirmation and Stage-2 safe nutrition handoff.

Changed:
- split meal-photo confirmation into inventory confirmation first and diary-save confirmation second;
- blocked diary save until nutrition is derived only from confirmed components;
- updated the CURRENT_STATE verification base to project main `b1d540bb40e93e8ec56ab41e02c0bacfebd566d0`.

Safety:
- provider requests during validation: 0;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged.

## 1.2.3 - 2026-07-09

Added:
- component-grounded Stage-1 visual inventory contract for meal-photo analysis;
- strict local validator for Stage-1 vision output;
- offline mixed-plate quality fixtures and thresholds.

Changed:
- rejected model-generated aggregate nutrition from the vision path;
- blocked low-confidence or invalid vision output from staging a diary-ready pending meal;
- updated the CURRENT_STATE verification base to project main `4aa67def8b4ece2aab6bb0ebdeb121318ccc7eab`.

Safety:
- provider requests during validation: 0;
- production remains on Gemini;
- Qwen remains not deployed and not active in production;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged.

## 1.2.2 — 2026-07-09

Added:
- recorded the exact-main Qwen live activation attempt outcome;
- recorded that synthetic probe succeeded while live Telegram food recognition quality failed;
- recorded the clean rollback to the previous Gemini production image;
- recorded the R7A forensic classification and remediation-only next step.

Changed:
- updated the CURRENT_STATE verification base to project main `22ed9e4d103b192947902fb66d6ad633b4d3ee31`;
- changed Qwen state from implemented and not yet deployed to activation attempted, rejected on quality, rolled back;
- clarified that current production routing is back on Gemini and Qwen is not active in production.

Safety:
- no provider requests were performed in this tracked-change step;
- no production config, build, deploy or restart actions were performed in this tracked-change step;
- production DB remained unchanged for this tracked-change step;
- Qdrant remained unchanged;
- no secrets, private IDs, raw provider responses or raw Telegram artifacts were stored.

## 1.2.1 — 2026-07-09

Added:
- verified external access for `qwen3-vl-8b-instruct`;
- recorded confirmed image-understanding success for the verified Qwen3 model;
- recorded confirmed `qwen2.5-vl-7b-instruct` model-specific `access_denied`.

Changed:
- corrected the tracked Qwen vision target model identifier to `qwen3-vl-8b-instruct`;
- updated the CURRENT_STATE verification base to project main `3cac5ecf6b47671d57675f2c26995d5ab97370f1`;
- clarified that credential replacement is not required and production remains on Gemini.

Safety:
- production unchanged;
- build/deploy/restart not performed;
- provider requests and Telegram smoke not performed in this tracked-change step;
- DB/Qdrant unchanged;
- no secrets, private IDs or raw provider responses stored.

## 1.2.0 — 2026-07-08

Added:
- current-main Qwen vision integration state;
- source and integration implementation commit identifiers;
- task-scoped `QWEN_API_KEY` routing status;
- local validation results for focused, related and agent-check suites.

Changed:
- V2-R1 status moved from in-progress to implemented and locally validated;
- CURRENT_STATE verification base moved to project main `60f84093c0fe82d29814c2ac8e3c0fb6dc847e7b`;
- next allowed V2-R1 sequence is expressed without temporary Draft PR state so it remains true after merge.

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
