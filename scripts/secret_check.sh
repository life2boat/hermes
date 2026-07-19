#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "== Staged Git-object secret scan =="

python3 - <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

from scripts.git_object_secret_policy import (
    GitObjectAcquisitionError,
    aggregate_exit_code,
    list_index_candidate_entries,
    scan_descriptors,
)


repository_root = Path.cwd()

try:
    entries = list_index_candidate_entries(repository_root)
    outcomes = scan_descriptors(
        repository_root=repository_root,
        descriptors=entries,
    )
except GitObjectAcquisitionError as exc:
    print("secret_check: Git index inspection failed", file=sys.stderr)
    print(
        f"  - caller=repository class=INTERNAL_ERROR code={exc.code}",
        file=sys.stderr,
    )
    sys.exit(2)
except Exception:
    print("secret_check: Git index inspection failed", file=sys.stderr)
    print(
        "  - caller=repository class=INTERNAL_ERROR "
        "code=GIT_OBJECT_SCAN_INTERNAL_ERROR",
        file=sys.stderr,
    )
    sys.exit(2)

denied = [outcome for outcome in outcomes if not outcome.clean]
if denied:
    print("secret_check: staged Git object denied", file=sys.stderr)
    for outcome in denied:
        size = "unknown" if outcome.size is None else str(outcome.size)
        print(
            "  - "
            f"path={outcome.descriptor.path} "
            f"mode={outcome.descriptor.mode} "
            f"caller=repository "
            f"class={outcome.exit_class} "
            f"result={outcome.result.value} "
            f"size={size}",
            file=sys.stderr,
        )
    sys.exit(aggregate_exit_code(outcomes))

if not outcomes:
    print("secret_check: no staged candidate Git objects to scan")
else:
    print(
        "secret_check: staged candidate Git objects passed "
        "the shared fail-closed policy"
    )
PY
