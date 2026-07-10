# CURRENT_STATE changelog

## 1.2.10 - 2026-07-10

Added:
- recorded the Qwen benchmark-context correction evidence path `/home/hermes/evidence/s71v2-r7f-q2-a-qwen-context-alignment/20260710T011214Z`;
- recorded Q1 execution path as `REPOSITORY_COMPONENTS_WITH_EXTERNAL_HARNESS`;
- recorded Q1 benchmark context as `TASK_SCOPED_DASHSCOPE_OPENAI_COMPATIBLE`, with credential mechanism `QWEN_API_KEY` and endpoint family `DASHSCOPE_INTL`;
- recorded the current built-in Hermes Qwen runtime context as `QWEN_OAUTH_PORTAL_CONTEXT`.

Changed:
- updated the CURRENT_STATE verification base to project main `1e048a7479253283ba2087e4e2ef6ad9ca584556` for repository-state/docs closure only;
- clarified that the historical Q1 access/schema and `qwen3.7-plus` benchmark results remain valid, but were produced through a task-scoped DashScope-compatible benchmark harness rather than the current built-in Hermes Qwen OAuth runtime;
- clarified that `qwen3.6-plus` and `qwen3.6-flash` remain `ACCESS_SCHEMA_PASS` / `not_benchmarked` in the same external benchmark context only;
- clarified that current Hermes Qwen runtime compatibility is not proven, deployable Qwen integration is not proven, eligible providers remain `none`, and deployment remains blocked.

Safety:
- provider requests during this docs task: 0;
- runtime/test/config changes during this docs task: 0;
- production build/deploy/restart not performed;
- production DB and Qdrant unchanged.

## 1.2.9 - 2026-07-10

Added:
- recorded next-generation Qwen access/schema success for `qwen3.7-plus`,
  `qwen3.6-plus`, and `qwen3.6-flash` against approved main
  `1b8a98195bc15e5dc0bfc54b71d308c77b86e627`;
- recorded that only `qwen3.7-plus` received the full three-image benchmark
  under the pre-approved fixed access-priority ordering;
- recorded next-generation Qwen benchmark evidence path
  `/home/hermes/evidence/s71v2-r7f-q1-qwen-nextgen/20260709T161257Z`.

Changed:
- updated the CURRENT_STATE verification base to project main
  `1b8a98195bc15e5dc0bfc54b71d308c77b86e627`;
- replaced the prior limited `qwen3-vl-8b-instruct` summary with the newer
  next-generation Qwen access audit semantics;
- clarified that all three tested next-generation aliases were operationally
  reachable and schema-valid on the access asset, but only `qwen3.7-plus` was
  quality-benchmarked;
- recorded `qwen3.7-plus` benchmark metrics as major precision `0.111111`,
  major recall `0.444444`, sauce recall `0.5`, confirmation correctness
  `1.000`, ambiguity gate pass `true`, aggregate nutrition violations `0`, and
  invalid staging `0`;
- recorded final classification `NEXTGEN_QWEN_FAIL_CLOSED_COMPATIBLE`,
  `benchmark_candidate=false`, eligible providers `none`, deployment blocked,
  and production remaining on the existing Gemini deployment state only.

Safety:
- provider requests during the recorded evidence: 6 total (3 access probes, 3
  benchmark requests), with 0 retries, 0 fallbacks and 0 repair requests;
- provider requests during this docs task: 0;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged.

## 1.2.8 - 2026-07-09

Added:
- recorded the limited exact-main vision re-benchmark against approved main `14981980403da56db94c90483bcab4ee209e9784`;
- recorded the single-request Gemini operational result `GEMINI_ACCESS_DENIED` at `PROVIDER_HTTP` with HTTP class `4xx`;
- recorded Qwen limited re-benchmark quality metrics and fail-closed ineligibility from the same evidence set.

Changed:
- updated the CURRENT_STATE verification base to project main `14981980403da56db94c90483bcab4ee209e9784`;
- replaced the older six-request benchmark blocker summary with the newer four-request limited re-benchmark result;
- clarified that no provider is eligible for rollout, automatic provider selection remains false, and production stays on the existing Gemini deployment state only.
- clarified that the three-image limited re-benchmark is bounded evidence for rollout eligibility, not a general quality verdict.

Safety:
- provider requests during the limited re-benchmark evidence: 4 total (1 Gemini, 3 Qwen), with 0 retries, 0 fallbacks and 0 repair requests;
- provider requests during this docs task: 0;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged.

## 1.2.7 - 2026-07-09

Added:
- shorter provider-neutral Stage-1 food-vision prompt contract;
- deterministic local confirmation derivation for mixed plates, sauces, low confidence, warnings, uncertainty, missing weights, broad ranges, and ambiguous normalization;
- provider-free replay coverage for prompt neutrality and local ambiguity calibration.

Changed:
- updated the CURRENT_STATE verification base to project main `0e176d0bc8db06d0443be049aa62855ebed9db51`;
- reduced benchmark-specific anchoring in the prompt and moved confirmation decisions out of prompt wording into local application logic;
- kept historical R7D-B benchmark evidence unchanged while clarifying that Qwen live quality is not yet revalidated and Gemini compatibility remains unproven.

Safety:
- provider requests during validation: 0;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged;
- strict schema validation, aggregate nutrition rejection, retry=0, and fallback=0 remained unchanged.


## 1.2.6 - 2026-07-09

Added:
- sanitized Gemini failure diagnostic contract with allowlisted execution stages and categories;
- provider-free Gemini request-shape compatibility coverage for the native adapter path;
- provider-free classification tests for wrapped HTTP, transport, decode, content-extraction and inventory-validation failures.

Changed:
- updated the CURRENT_STATE verification base to project main `7b38d862978781b711b1ca5d76e1735bc7ee0d27`;
- preserved typed Gemini stage/category metadata before redacting raw provider details;
- kept historical Gemini benchmark evidence unchanged as `GEMINI_UNKNOWN_OPERATIONAL_FAILURE`;
- clarified that future Gemini retests may yield narrower safe categories, but live Gemini compatibility remains unproven.

Safety:
- provider requests during validation: 0;
- production config/build/deploy/restart not performed;
- production DB and Qdrant unchanged;
- raw provider errors, raw responses, keys and image payloads remained redacted.


## 1.2.5 - 2026-07-09

Added:
- recorded the exact-main Stage-1 food vision provider benchmark against three approved sanitized assets;
- recorded exact benchmark request accounting and the audit image digest;
- recorded provider classifications for Gemini and Qwen from the same offline benchmark.

Changed:
- updated the CURRENT_STATE verification base to project main `10543bf2ad05c518f202eb23bc52fcd45dfa25e6`;
- promoted the benchmark result to the top active blocker because no provider met the Stage-1 rollout gate;
- updated the active-work section from R7C implementation state to R7D-B benchmark state.

Safety:
- provider requests during validation: 6 total (3 Gemini, 3 Qwen), with 0 retries, 0 fallbacks and 0 repair requests;
- Telegram requests, diary writes, production DB opens/writes and Qdrant requests remained 0;
- production build/deploy/restart not performed and production runtime remained unchanged;
- no secrets, raw provider responses or raw provider errors were stored.

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
