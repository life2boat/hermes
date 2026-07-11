# Trusted Source, Environment, and Vision Pre-Deploy Contract

## Purpose

This runbook defines the minimum source-provenance, environment-precedence, and
vision-routing gates required before building or deploying Hermes/HealBite.
Passing these gates does not mean production has been updated.

## Sprint 7.1A2 provenance snapshot

- Trusted project remote: healbite-project (life2boat/hermes).
- Trusted base: project main at
  6f21b4b15dfcf53d67a5dd200de7ca406ef57569.
- Feature branch: healbite-s71a2-foundation-blockers.
- Isolated worktree:
  /home/hermes/.hermes/worktrees/healbite-s71a2-foundation-blockers.
- Deployed revision observed read-only:
  20f1469dc395130fbde30b9736750e247e9b8306.
- The deployed revision is an ancestor of the trusted base.
- The canonical checkout was divergent and dirty at audit time: 790 tracked
  changes and 4 untracked paths. It was not modified by this sprint.

The canonical checkout is not an approved build or deploy source while it is
dirty or divergent. Do not copy files from it into a release worktree.

## Trusted development and deploy source

1. Fetch the project remote.
2. Resolve the expected project-main SHA.
3. Create a new branch and worktree directly from that exact commit.
4. Require an empty git status --porcelain=v1.
5. Require every changed source, test, and documentation file to be tracked.
6. Verify the expected deployed-to-main ancestry.
7. Build only from the clean exact-SHA worktree.

Never repair a dirty checkout with automated reset, clean, checkout, rebase, or
file copying. Preserve it for a separate provenance review.

## Environment precedence

Normal runtime loading follows this order:

1. Values already present in the process/container environment.
2. Missing values from HERMES_HOME/.env.
3. Still-missing values from the project .env.
4. Explicit external secret-source policy, if configured.
5. Code defaults for still-unset non-secret options.

Dotenv files use fill-missing semantics. They must not replace a process value,
including when the dotenv value is empty. Repeated loading is idempotent.

An external secret manager may replace an existing value only when its own
explicit override_existing policy is enabled. This is separate from ordinary
dotenv loading and must be reviewed as a credential operation.

Never print secret values, fragments, or lengths during a precedence audit.
Compare sources only through presence metadata or irreversible fingerprints.

Duplicate assignments in active, untracked dotenv files require an operator
review. Do not edit, normalize, or delete those files as part of a source-code
change.

## Explicit vision provider contract

An explicitly configured vision provider is authoritative:

- if its client resolves, use that client;
- if it is unavailable and provider fallback was not explicitly enabled,
  stop before any external request;
- do not instantiate or select an auto backend implicitly;
- return a controlled error without credentials or internal exception text;
- apply the same behavior to synchronous and asynchronous calls.

The existing provider=auto mode may use the configured auto-resolution chain.
An explicit provider may use that chain only when the caller supplies an
explicit call policy with fallback_provider=True.

The single-request food-vision policy disables provider fallback, retries,
credential recovery, and model fallback.

## Pre-deploy verification

Before a production rollout:

1. Confirm the worktree HEAD equals the approved SHA.
2. Confirm the worktree is clean and the remote is the project repository.
3. Confirm the deployed revision is an expected ancestor or approved target.
4. Run the environment-precedence matrix.
5. Run sync and async explicit-vision fail-closed tests.
6. Run related provider and Telegram tests with network access disabled.
7. Run scripts/agent_check.sh.
8. Scan the diff for credentials and untracked runtime files.
9. Confirm no provider request occurred during validation.
10. Confirm production containers, databases, configuration, and Qdrant were
    not changed by validation.

Production build, deployment, restart, configuration edits, and credential
cleanup require a separate approved rollout task.
