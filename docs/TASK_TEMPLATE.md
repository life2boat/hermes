# Hermes / HealBite Engineering Task Template

Use this template for repository, release or operational work. Delete sections
that are genuinely inapplicable, but do not delete safety or evidence fields to
hide an unknown. Values such as `PASS` must be supported by deterministic
evidence.

```text
# TASK: <concise outcome>

MODEL_RECOMMENDATION=<optional>
REASONING_LEVEL=<optional>

MODE=DISCOVERY_ONLY|IMPLEMENTATION|VALIDATION|RELEASE|PRODUCTION_OPERATION
FAST_TRACK=true|false
AUDIT_REQUIRED=true|false
PRODUCTION_EXECUTION_ALLOWED=true|false

CANONICAL_REPOSITORY=https://github.com/life2boat/hermes.git
CANONICAL_REMOTE=github
CANONICAL_MAIN_REF=refs/remotes/github/main
EXPECTED_BASE_SHA=<40-char SHA or CURRENT_CANONICAL_MAIN>

============================================================
GOAL
============================================================

<One measurable end state.>

DELIVERABLES:
- <file, behavior, evidence, PR, build or operation>

STOP_BOUNDARY:
<Exact last authorized state, for example Draft PR created; do not merge.>

============================================================
CONTEXT
============================================================

CURRENT_CONFIRMED_STATE:
- <facts backed by canonical code/current evidence>

HISTORICAL_OR_UNVERIFIED_CONTEXT:
- <old PR, report or assumption that must be rechecked>

AUTHORITATIVE_SOURCES:
- AGENTS.md
- docs/HERMES_SOURCE_MAP.md
- docs/HERMES_SYSTEM_MODEL.md
- docs/HERMES_INVARIANTS.md
- <applicable code, test, ADR, skill, policy or runbook>

============================================================
CONSTRAINTS
============================================================

ALLOWED:
- <read/write/network/external actions>

FORBIDDEN:
- <production, DB, Qdrant, secret, send, merge or destructive actions>

FILES_IN_SCOPE:
- <paths>

FILES_OUT_OF_SCOPE:
- <paths/components>

DATA_AND_PRIVACY:
- Do not print or commit secrets, user/chat IDs, health data, messages, raw
  production logs or correlation identifiers.

============================================================
RISKS AND INVARIANTS
============================================================

AFFECTED_INVARIANTS:
- <ID/title from docs/HERMES_INVARIANTS.md>

PRIMARY_RISKS:
- <failure mode> -> <prevention/evidence>

FAIL_CLOSED_CONDITIONS:
- <condition that requires FAIL/BLOCKED/INCONCLUSIVE and stop>

ROLLBACK_OR_RECOVERY:
- <required only for authorized mutations; otherwise NOT APPLICABLE>

============================================================
DISCOVERY / PROVENANCE
============================================================

1. Fetch and verify the canonical remote/main.
2. Record exact base SHA, branch and clean isolated worktree.
3. Trace current entry point -> controller/router -> service/store -> tests.
4. Inspect relevant history, ADR, skill and current-state records.
5. Classify gaps as CONFIRMED_CURRENT, CONFIRMED_HISTORICAL, PLANNED,
   UNKNOWN or INCONCLUSIVE.

REQUIRED_PROVENANCE_EVIDENCE:
- CANONICAL_MAIN_SHA=
- HEAD_SHA=
- BRANCH=
- WORKTREE_CLEAN=true|false
- EVIDENCE_FILES=
- EVIDENCE_TESTS=
- EVIDENCE_COMMITS=

============================================================
IMPLEMENTATION
============================================================

IMPLEMENTATION_ALLOWED=true|false

CHANGE_PLAN:
1. <smallest coherent change>
2. <tests/negative cases>
3. <docs/state update>

BEHAVIOR_NOT_TO_CHANGE:
- <adjacent contract>

DOCUMENTATION_UPDATES:
- docs/CURRENT_STATE.md: REQUIRED|NOT_REQUIRED because <reason>
- docs/CURRENT_STATE_CHANGELOG.md: REQUIRED|NOT_REQUIRED
- ADR: REQUIRED|NOT_REQUIRED because <reason>
- Skill/runbook: REQUIRED|NOT_REQUIRED because <reason>

============================================================
VALIDATION
============================================================

Run only applicable checks and report exact outcomes:

- SECRET_SCAN: PASS|FAIL|NOT_RUN|INCONCLUSIVE
- FOCUSED_TESTS: PASS|FAIL|NOT_RUN|INCONCLUSIVE
- RELATED_TESTS: PASS|FAIL|NOT_RUN|INCONCLUSIVE
- AGENT_CHECK: PASS|FAIL|NOT_RUN|INCONCLUSIVE
- DIFF_CHECK: PASS|FAIL|NOT_RUN|INCONCLUSIVE
- EXACT_HEAD_CI: PASS|FAIL|PENDING|NOT_RUN|INCONCLUSIVE
- MANUAL_SMOKE: PASS|FAIL|NOT_PERFORMED|INCONCLUSIVE

REQUIRED_TEST_COMMANDS:
- bash scripts/secret_check.sh
- scripts/run_tests.sh <focused test paths>
- bash scripts/agent_check.sh
- git diff --check

Do not report PASS for a check not executed against the final change/commit.

============================================================
DELIVERY
============================================================

DELIVERY_TARGET=LOCAL_DIFF|COMMIT|DRAFT_PR|READY_PR|MERGE|BUILD|DEPLOY
BRANCH=<codex/...>
COMMIT_MESSAGE=<type: concise outcome>
PR_TITLE=<title>
PR_MODE=DRAFT|READY|NOT_APPLICABLE
MERGE_ALLOWED=true|false
BUILD_ALLOWED=true|false
DEPLOY_ALLOWED=true|false

============================================================
FINAL REPORT
============================================================

STATUS=PASS|FAIL|BLOCKED|INCONCLUSIVE
BASE_SHA=
BRANCH=
WORKTREE=
FILES_CHANGED=
TESTS=
VALIDATION=
COMMIT_SHA=
PUSH=PASS|FAIL|NOT_PERFORMED
PR_NUMBER=
PR_URL=
BUILD=PASS|FAIL|NOT_PERFORMED|INCONCLUSIVE
DEPLOY=PASS|FAIL|NOT_PERFORMED|INCONCLUSIVE
PRODUCTION_CHANGED=true|false
DATABASE_CHANGED=true|false
QDRANT_CHANGED=true|false
SECRETS_CHANGED=true|false
BLOCKING_ISSUES=
REMAINING_RISKS=
NEXT_ACTION=
```

## Task quality gate

A task is ready for execution when another engineer can determine, without chat
history, what success means, what is forbidden, which evidence is required and
where execution must stop. If the task author cannot state those boundaries,
the next phase is discovery/planning rather than implementation or production
mutation.
