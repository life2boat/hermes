# RUNBOOK_CODING_LOOP

## When To Use /coding-loop

Use `/coding-loop` for bounded coding work in Hermes/HealBite when the task needs a repeatable engineering cycle: plan, edit, test, review, and report. Typical cases: bug fixes, refactors, test additions, migration prep, deploy prep, and checkpoint work.

## How To Run scripts/agent_check.sh

Run from the project root:

```bash
cd /home/hermes/.hermes/hermes-agent
bash scripts/agent_check.sh
```

The script compiles key Python files, runs Ruff if available, and executes the targeted pytest set used by the current Hermes/HealBite workflow.

## Private Artifacts Policy

- `backups/`, `memory_capsules/`, `.env` snapshots, database dumps, and auth files must never live in the git working tree.
- Use `/home/hermes/private_backups/hermes-agent/` and keep it protected with `chmod 700`.
- Do not print secret values.
- Do not commit raw memory capsules.
- Useful knowledge from capsules may only be saved as sanitized docs in a separate reviewable commit.
- `scripts/secret_check.sh` must run before compile, lint, and tests via `scripts/agent_check.sh`.

## Hook Bootstrap

The tracked `lefthook.yml` config includes a `pre-commit` `secret-check` command for `bash scripts/secret_check.sh`.

For a new checkout, install or reinstall local git hooks from the project root:

```bash
cd /home/hermes/.hermes/hermes-agent
venv/bin/lefthook install
```

To verify the hook path manually:

```bash
cd /home/hermes/.hermes/hermes-agent
venv/bin/lefthook run pre-commit --verbose
```

Fallback if hooks are not installed yet:

```bash
bash scripts/secret_check.sh
bash scripts/agent_check.sh
```

Local `.git/hooks` files are not versioned and must be installed or reinstalled in each checkout.

## How To Set CHANGED_FILES

Use `CHANGED_FILES` when you want the compile step to focus on specific Python files:

```bash
cd /home/hermes/.hermes/hermes-agent
CHANGED_FILES="gateway/run.py gateway/config.py agent/auxiliary_client.py" bash scripts/agent_check.sh
```

Keep the value space-separated. Only include Python files that should be sent to `python3 -m py_compile`.

## Commands That Require User Confirmation

Do not run these without explicit user approval:

- destructive SQL or any operation that can delete/overwrite user data;
- `rm -rf`, `git reset --hard`, broad `git restore`, or backup deletion;
- secret, token, provider, model, admin-list, or production config changes;
- any deploy that changes production behavior when the user did not request deploy.

## Deploy Checkpoint

Before a deploy checkpoint:

1. Confirm what changed and whether runtime code was touched.
2. Create or confirm a backup if production code/data is involved.
3. Run `bash scripts/agent_check.sh`.
4. If relevant, run any extra targeted pytest near the changed files.
5. Only rebuild/restart containers when the user explicitly asked for deploy or runtime rollout.
6. After deploy, verify `hermes-bot` is running, `restart_count=0`, logs contain no traceback, and user-facing paths do not leak provider/auth details.
