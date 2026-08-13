# Antigravity onboarding for Hermes / HealBite

Use this read-only onboarding when opening a fresh Hermes workspace in Google
Antigravity. It grounds the agent in repository source-of-truth without copying
historical conversations or creating a second project state.

The workspace must expose:

- `AGENTS.md`;
- `.agents/rules/hermes-core.md`;
- `.agents/skills/hermes-task/SKILL.md`;
- `docs/HERMES_SOURCE_MAP.md` and `docs/CURRENT_STATE.md`;
- `scripts/prepare_task.py`.

If Antigravity does not automatically mark `hermes-core` as Always On, open
Customizations, select the workspace rule, and set its activation to Always On.
The repository intentionally has no `GEMINI.md`: Antigravity already parses the
canonical root `AGENTS.md`, and a second bridge would duplicate instructions.

## First onboarding prompt

Paste this into a new Agent task:

```text
TASK=Hermes / HealBite read-only workspace onboarding

Use the workspace rule hermes-core and the hermes-task skill for discovery only.

Establish the canonical repository, remote, main ref, fetched main SHA, local
HEAD SHA, branch, and worktree cleanliness from Git evidence. Read AGENTS.md,
docs/HERMES_SOURCE_MAP.md, docs/CURRENT_STATE.md, docs/HERMES_SYSTEM_MODEL.md,
docs/HERMES_INVARIANTS.md, docs/TASK_LIFECYCLE.md,
docs/AI_AGENT_RULEBOOK.md, the Knowledge Pack indexes, and relevant ADRs.
Run `python scripts/prepare_task.py` and inspect its stdout.

Do not modify the repository. Do not access or mutate production. Make no
provider calls. Do not mutate a database, Qdrant, secrets, credentials, feature
flags, containers, or deployments. Do not infer runtime health from repository
documentation. Stop after the grounding report.

Return exactly these fields, using UNKNOWN when fresh permitted evidence cannot
establish a value and never forcing PASS:

STATUS=
CANONICAL_REPOSITORY=
CANONICAL_REMOTE=
CANONICAL_MAIN_REF=
CANONICAL_MAIN_SHA=
LOCAL_HEAD_SHA=
WORKTREE_CLEAN=
AGENTS_MD_READ=
CURRENT_STATE_READ=
PREPARE_TASK=
ARCHITECTURE_UNDERSTOOD=
ENGINEERING_SYSTEM_UNDERSTOOD=
PRODUCTION_BOUNDARY_UNDERSTOOD=
CURRENT_QWEN_STATE=
CURRENT_MEMORY_STATE=
CURRENT_HOUSEHOLD_STATE=
CURRENT_PRODUCTION_STATE=
TOP_ENGINEERING_INVARIANTS=
CONTEXT_GAPS=
READY_FOR_HERMES_IMPLEMENTATION=
```

`READY_FOR_HERMES_IMPLEMENTATION=PASS` means only that repository grounding is
complete. It grants no mutation, provider, production, or deployment authority.

## Operating ownership

- Primary execution agent: Antigravity.
- Reserve executor: Codex, after an explicit ownership handoff.
- Research and assurance: Manus, read-only.

Only one executor may mutate a branch or worktree. A new implementation task
must state the current mutation owner before changes begin.
