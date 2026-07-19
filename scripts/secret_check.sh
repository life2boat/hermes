#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "== Tracked/staged secret scan =="

python3 - <<'PY'
from __future__ import annotations

import re
from collections import defaultdict
import subprocess
import sys
from pathlib import Path

from scripts.secret_scanner import scan_secret_text

repo_root = Path.cwd()

def _diff_text(args: list[str]) -> str:
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout

def _added_lines(diff_text: str) -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    current_file: str | None = None
    new_lineno = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:]
            continue
        if raw.startswith("@@ "):
            match = re.search(r"\+(\d+)(?:,(\d+))?", raw)
            new_lineno = int(match.group(1)) if match else 0
            continue
        if current_file is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            rows.append((current_file, new_lineno, raw[1:]))
            new_lineno += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            continue
        elif raw.startswith(" "):
            new_lineno += 1
    return rows

changed_lines = _added_lines(_diff_text([
    "git", "diff", "--unified=0", "--diff-filter=ACMR", "--no-ext-diff",
]))
changed_lines.extend(_added_lines(_diff_text([
    "git", "diff", "--cached", "--unified=0", "--diff-filter=ACMR", "--no-ext-diff",
])))

findings: list[tuple[str, int, str]] = []
seen: set[tuple[str, int, str]] = set()

if not changed_lines:
    print("secret_check: no changed tracked/staged lines to scan")
    sys.exit(0)

changed_by_file: dict[str, list[tuple[int, str]]] = defaultdict(list)
for rel, lineno, line in changed_lines:
    changed_by_file[rel].append((lineno, line))

for rel, rows in changed_by_file.items():
    added_text = "\n".join(line for _, line in rows)
    first_lineno = min(lineno for lineno, _ in rows)
    for finding in scan_secret_text(added_text):
        item = (rel, first_lineno, finding.rule_id)
        if item not in seen:
            seen.add(item)
            findings.append(item)

if findings:
    print("secret_check: potential tracked secrets detected:", file=sys.stderr)
    for rel, lineno, label in findings:
        print(f"  - {rel}:{lineno} [{label}]", file=sys.stderr)
    sys.exit(1)

print("secret_check: no high-confidence secrets found in changed tracked/staged lines")
PY
