# Context-hygiene preservation map

This map records the ownership of invariants moved out of permanently loaded
instruction files. It changes instruction loading, not runtime behavior or
release authority.

| Invariant | Old location | New location | Executable validator | Semantics changed |
| --- | --- | --- | --- | --- |
| Canonical repository, clean source, exact main | `skills/deploy/SKILL.md` | `skills/deploy/references/provenance.md` | `scripts/hermes_production_deploy.sh check-repository` | false |
| Immutable digest, OCI revision, image secret scan | `skills/deploy/SKILL.md` | `skills/deploy/references/image-attestation.md` | `scripts/attest_remote_registry_image.py`, `scripts/hermes_image_secret_scan.py` | false |
| Explicit authority and fail-closed technical gates | `skills/deploy/SKILL.md` | `skills/deploy/references/authority.md` | authority package and deployment validators | false |
| Live DB path, zero writers, fresh verified backup, SQLite checks | `skills/deploy/SKILL.md` | `skills/deploy/references/database-safety.md`, `backup-restore.md` | staged migration/deploy validators | false |
| Expected/effective migration scope and rehearsal | `skills/deploy/SKILL.md` | `skills/deploy/references/migration.md` | `scripts/hermes_staged_schema_migrate.py` | false |
| Exact rollback and legacy bootstrap | `skills/deploy/SKILL.md` | `skills/deploy/references/rollback.md` | deployment and bootstrap validators | false |
| Runtime, security, and Qdrant non-interference verification | `skills/deploy/SKILL.md` | `skills/deploy/references/runtime-verification.md` | `scripts/hermes_post_deploy_attestation.py` | false |
| SQLite authority, user/household isolation, durable outbox | `skills/memory/SKILL.md` | `skills/memory/references/authority.md`, `convergence.md` | Memory OS and hydration tests | false |
| Fact UUID and epoch safety | `skills/memory/SKILL.md` | `skills/memory/references/epoch-safety.md` | identity/migration tests | false |
| Qdrant dry-run/rebuild/cutover boundary | `skills/memory/SKILL.md` | `skills/memory/references/qdrant-rebuild.md`, `restore.md` | `scripts/rebuild_qdrant_memory_index.py` | false |
| Telegram ownership, no-send diagnostics, token privacy | `skills/telegram/SKILL.md` | `skills/telegram/references/safety-invariants.md` | focused Telegram/gateway tests | false |
| Telegram runtime troubleshooting | `skills/telegram/SKILL.md` | `skills/telegram/references/runtime-diagnostics.md` | `scripts/healbite`, focused Telegram tests | false |
| Current-state, lifecycle, failure-capture selection | `AGENTS.md` | compact context router in `AGENTS.md` | documentation/link checks | false |
| Complex PromptSpec and task-prompt structure | `AGENTS.md`, `docs/TASK_TEMPLATE.md` | `docs/TASK_TEMPLATE.md`, `ai_engineering/prompt_contracts.py` | prompt-system tests | false |
