# Deployment authority and gate semantics

Operator authorization is an explicit input, never an output of a generator,
plan, CI job, or review. Production mutation requires task-scoped authority and
the selected canonical procedure.

For a staged migration, use one non-reusable operation ID and the canonical
approval → clean-start → plan → reviewed companion evidence → final authority
→ package validation lifecycle. Preserve the full migration registry while
binding the separately authorized expected mutation subset.

Report governance-only warnings without converting a complete technical PASS
into a failure. Never waive technical fail-closed gates: any `FAIL`,
`UNKNOWN`, or absent result blocks mutation.
