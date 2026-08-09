# ADR-005: Versioned AI Engineering Knowledge System

## Problem

AI-assisted development loses reliability when architecture, safety decisions,
failure lessons, and task boundaries live only in chat history or broad passive
instructions. A new agent can write syntactically valid code while missing the
repository's actual authorities, invariants, or stop boundary.

## Context

Phase 0, merged in commit `c6d0852b95a068a3bab7528e656da91ab4274a08`,
introduced `HERMES_SOURCE_MAP.md`, `HERMES_SYSTEM_MODEL.md`,
`HERMES_INVARIANTS.md`, `AI_AGENT_RULEBOOK.md`, and `TASK_TEMPLATE.md`.
The Phase 0.5 usability check demonstrated that a new agent can reconstruct the
system architecture, forbidden changes, deployment gates, and task workflow
from those documents alone.

The repository already has executable code, tests, policies, procedural skills,
runbooks, and current-state records. The knowledge layer must organize and link
those authorities rather than duplicate or override them.

## Decision

Treat Hermes as an AI-assisted engineering system with a small, versioned,
repository-native knowledge architecture.

- Keep a source map, system model, invariant catalog, agent rulebook, and task
  template as the entry point for a new agent.
- Record durable architectural choices as ADRs with problem, context, decision,
  alternatives, and consequences.
- Organize supporting knowledge into architecture, decisions, failures,
  patterns, operations, and AI-agent lessons.
- Link to authoritative code, tests, skills, runbooks, and current state instead
  of copying their detailed procedures.
- Update `CURRENT_STATE.md` when work changes the verified repository state.
- Keep executable contracts authoritative when prose and implementation differ;
  correct stale documentation as part of the change.
- Do not introduce a new runtime service, vector database, or hidden agent
  memory for this phase. The Knowledge Pack is a versioned documentation layer.

## Alternatives Considered

- **Rely on chat history and operator memory.** Rejected because it is not
  reviewable, versioned, or reliably available to a new session.
- **Put all guidance in one large `AGENTS.md`.** Rejected because navigation,
  authority precedence, historical decisions, and focused maintenance become
  difficult.
- **Create an AI retrieval service immediately.** Rejected because the project
  first needs stable, reviewed knowledge artifacts; a new runtime would add
  behavior and another authority boundary.
- **Write descriptive docs without tests or operational links.** Rejected
  because prose alone cannot prove implementation or production compliance.

## Consequences (+ and -)

**Positive**

- New agents can orient themselves without reconstructing architecture from the
  entire codebase or old conversations.
- Decisions and failure lessons become reviewable Git history.
- Task prompts can name explicit authorities, evidence, and stop boundaries.
- Documentation can point agents toward executable contracts instead of
  encouraging speculative behavior.

**Negative**

- The knowledge layer requires maintenance whenever architecture or authorities
  change.
- Duplicate prose can drift unless documents link to one canonical source.
- A complete-looking document can create false confidence; tests and live
  evidence remain mandatory.
- More structure adds review overhead for small changes, so records should stay
  concise and decision-focused.
