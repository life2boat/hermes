#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "== Tracked/staged secret scan =="

python3 - <<'PY'
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

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

patterns = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "telegram-token-assignment",
        re.compile(r'(?i)\b(?:TELEGRAM_BOT_TOKEN|BOT_TOKEN)\b\s*[:=]\s*[\'"]?\d{8,}:[A-Za-z0-9_-]{20,}'),
    ),
    (
        "openai-style-key-assignment",
        re.compile(r'(?i)\b(?:OPENAI_API_KEY|NOUS_API_KEY|ANTHROPIC_API_KEY|MISTRAL_API_KEY|API_KEY)\b\s*[:=]\s*[\'"]?sk-[A-Za-z0-9_-]{16,}'),
    ),
    (
        "github-token-assignment",
        re.compile(r'(?i)\b(?:GITHUB_TOKEN|GH_TOKEN|GITHUB_PAT|TOKEN)\b\s*[:=]\s*[\'"]?(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'),
    ),
    (
        "generic-secret-assignment",
        re.compile(r'(?i)\b(?:[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD))\b\s*[:=]\s*[\'"]?[A-Za-z0-9_:/\\-]{20,}'),
    ),
]

placeholder_markers = (
    "example",
    "placeholder",
    "changeme",
    "dummy",
    "sample",
    "your_",
    "your-",
    "<redacted>",
    "[redacted]",
    "***",
)

findings: list[tuple[str, int, str]] = []
seen: set[tuple[str, int, str]] = set()

if not changed_lines:
    print("secret_check: no changed tracked/staged lines to scan")
    sys.exit(0)

for rel, lineno, line in changed_lines:
    lower = line.lower()
    if any(marker in lower for marker in placeholder_markers):
        continue
    for label, pattern in patterns:
        if pattern.search(line):
            item = (rel, lineno, label)
            if item not in seen:
                seen.add(item)
                findings.append(item)
            break

if findings:
    print("secret_check: potential tracked secrets detected:", file=sys.stderr)
    for rel, lineno, label in findings:
        print(f"  - {rel}:{lineno} [{label}]", file=sys.stderr)
    sys.exit(1)

print("secret_check: no high-confidence secrets found in changed tracked/staged lines")
PY
