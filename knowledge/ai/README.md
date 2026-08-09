# AI Agent Knowledge

This directory stores reviewed prompt patterns, agent workflow lessons, and
evaluation notes for AI-assisted engineering. It is a documentation layer, not
a hidden runtime memory service.

Every new task should first prepare a repository-bound context package:

- [`scripts/prepare_task.py`](../../scripts/prepare_task.py)

Generated context belongs under the ignored `.task_context/` directory and is
local evidence, not a Git artifact or a test PASS receipt. Then review:

- [`docs/AI_AGENT_RULEBOOK.md`](../../docs/AI_AGENT_RULEBOOK.md)
- [`docs/TASK_TEMPLATE.md`](../../docs/TASK_TEMPLATE.md)
- [`docs/HERMES_SOURCE_MAP.md`](../../docs/HERMES_SOURCE_MAP.md)
- [`docs/HERMES_SYSTEM_MODEL.md`](../../docs/HERMES_SYSTEM_MODEL.md)
- [`docs/AI_REVIEW_CHECKLIST.md`](../../docs/AI_REVIEW_CHECKLIST.md)
- [`docs/PRODUCTION_READINESS_CHECKLIST.md`](../../docs/PRODUCTION_READINESS_CHECKLIST.md) when release or production readiness is in scope
- [`docs/HERMES_INVARIANTS.md`](../../docs/HERMES_INVARIANTS.md)

Lessons recorded here should identify the evidence behind them, distinguish
planning from implementation authority, and state the required stop boundary.
