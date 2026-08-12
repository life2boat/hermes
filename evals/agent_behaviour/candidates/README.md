# Failure-to-Eval Candidates

This directory is an evidence-only staging area for deterministic candidate
improvements produced from sanitized failure records and trace fixtures.

Candidate files are **not** GOLDEN corpus members. Their presence does not
change the Golden corpus digest and does not change `manifest.json`, datasets,
or trace fixtures. A candidate remains `CANDIDATE` with human review
`NOT_PERFORMED` and `promotion_authorized=false` until a separately reviewed
repository change updates a dataset, runs evals, passes PR CI, and is merged.

The offline builder refuses direct Golden promotion, direct production mutation,
unsafe evidence references, symlink escapes, and output overwrite. A candidate
can propose a later change; it cannot activate that change.
