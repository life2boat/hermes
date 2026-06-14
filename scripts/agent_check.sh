#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$SCRIPT_DIR/secret_check.sh"

CHANGED_FILES="${CHANGED_FILES:-}"

echo "== Python compile =="
if [ -n "$CHANGED_FILES" ]; then
  python3 -m py_compile $CHANGED_FILES
else
  python3 -m py_compile \
    gateway/run.py \
    gateway/config.py \
    gateway/platforms/telegram.py \
    gateway/platforms/healbite_memory_bridge.py \
    agent/prompt_builder.py \
    agent/auxiliary_client.py
fi

echo "== Ruff =="
if command -v ruff >/dev/null 2>&1; then
  ruff check gateway agent hermes_cli tools tests --output-format=concise
elif [ -x venv/bin/ruff ]; then
  venv/bin/ruff check gateway agent hermes_cli tools tests --output-format=concise
else
  echo "ruff not found, skipping"
fi

echo "== Targeted pytest =="
if [ -x venv/bin/python ]; then
  PY=venv/bin/python
else
  PY=python3
fi

$PY -m pytest -q \
  tests/gateway/test_telegram_memory_stats.py \
  tests/gateway/test_slash_access.py \
  tests/gateway/test_telegram_noise_filter.py \
  tests/gateway/platforms/test_healbite_memory_bridge.py \
  tests/agent/test_auxiliary_client.py \
  tests/agent/test_prompt_builder.py \
  tests/scripts/test_memory_analytics_report.py
