# Architecture Knowledge

This directory collects stable component maps and diagrams that help an agent
understand how Hermes is assembled. It describes boundaries and relationships;
it does not replace source code or executable contracts.

Start with:

- [`docs/HERMES_SYSTEM_MODEL.md`](../../docs/HERMES_SYSTEM_MODEL.md) for the
  runtime and data-flow model.
- [`docs/HERMES_SOURCE_MAP.md`](../../docs/HERMES_SOURCE_MAP.md) for the
  authoritative path map.
- [`docs/HERMES_INVARIANTS.md`](../../docs/HERMES_INVARIANTS.md) for boundaries
  that every architecture document must preserve.

Future diagrams should name their verified source SHA and link to the code or
policy that proves each important boundary.
