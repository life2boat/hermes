#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ -n "${HERMES_SECRET_CHECK_BASE_SHA:-}" || -n "${HERMES_SECRET_CHECK_SOURCE_SHA:-}" ]]; then
    echo "== Exact candidate Git-tree secret scan =="
else
    echo "== Staged Git-object secret scan =="
fi

python3 - <<'PY'
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from scripts.git_object_secret_policy import (
    GitObjectAcquisitionError,
    aggregate_exit_code,
    candidate_tree_entries,
    list_index_candidate_entries,
    scan_descriptors,
)


repository_root = Path.cwd()
base_sha = os.environ.get("HERMES_SECRET_CHECK_BASE_SHA", "")
source_sha = os.environ.get("HERMES_SECRET_CHECK_SOURCE_SHA", "")
range_requested = bool(base_sha or source_sha)

try:
    if range_requested:
        if not (
            re.fullmatch(r"[0-9a-f]{40}", base_sha)
            and re.fullmatch(r"[0-9a-f]{40}", source_sha)
        ):
            raise GitObjectAcquisitionError("GIT_OBJECT_SCAN_RANGE_INVALID")
        _source_entries, entries = candidate_tree_entries(
            repository_root=repository_root,
            approved_base_sha=base_sha,
            source_sha=source_sha,
        )
    else:
        entries = list_index_candidate_entries(repository_root)
    outcomes = scan_descriptors(
        repository_root=repository_root,
        descriptors=entries,
    )
except GitObjectAcquisitionError as exc:
    print("secret_check: Git object inspection failed", file=sys.stderr)
    print(
        f"  - caller=repository class=INTERNAL_ERROR code={exc.code}",
        file=sys.stderr,
    )
    sys.exit(2)
except Exception:
    print("secret_check: Git object inspection failed", file=sys.stderr)
    print(
        "  - caller=repository class=INTERNAL_ERROR "
        "code=GIT_OBJECT_SCAN_INTERNAL_ERROR",
        file=sys.stderr,
    )
    sys.exit(2)

denied = [outcome for outcome in outcomes if not outcome.clean]
if denied:
    denied_scope = (
        "exact candidate Git object" if range_requested else "staged Git object"
    )
    print(f"secret_check: {denied_scope} denied", file=sys.stderr)
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
    scope = "exact candidate" if range_requested else "staged candidate"
    print(f"secret_check: no {scope} Git objects to scan")
else:
    scope = (
        "exact candidate Git-tree"
        if range_requested
        else "staged candidate Git"
    )
    print(f"secret_check: {scope} objects passed the shared fail-closed policy")
PY
