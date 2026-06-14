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
modified_raw = subprocess.run(
    ["git", "diff", "--name-only", "-z", "--diff-filter=ACMR"],
    check=True,
    capture_output=True,
).stdout
staged_raw = subprocess.run(
    ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
    check=True,
    capture_output=True,
).stdout
entries = set(filter(None, modified_raw.split(b"\0") + staged_raw.split(b"\0")))

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

if not entries:
    print("secret_check: no changed tracked/staged files to scan")
    sys.exit(0)

for entry in sorted(entries):
    rel = Path(entry.decode("utf-8", "surrogateescape"))
    path = repo_root / rel
    if not path.is_file():
        continue
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    for lineno, line in enumerate(text.splitlines(), start=1):
        lower = line.lower()
        if any(marker in lower for marker in placeholder_markers):
            continue
        for label, pattern in patterns:
            if pattern.search(line):
                item = (rel.as_posix(), lineno, label)
                if item not in seen:
                    seen.add(item)
                    findings.append(item)
                break

if findings:
    print("secret_check: potential tracked secrets detected:", file=sys.stderr)
    for rel, lineno, label in findings:
        print(f"  - {rel}:{lineno} [{label}]", file=sys.stderr)
    sys.exit(1)

print("secret_check: no high-confidence secrets found in changed tracked/staged files")
PY
