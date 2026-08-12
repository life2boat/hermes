#!/usr/bin/env python3
"""Run the provider-free Prompt Quality corpus and emit a safe receipt."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ai_engineering.prompt_contracts import PromptContractError
from ai_engineering.prompt_system import (
    normalize_prompt_eval_result,
    run_prompt_evals,
)


def _write_new_json(path: Path, payload: dict[str, object]) -> None:
    if path.is_symlink() or not path.is_absolute() or not path.parent.is_dir():
        raise PromptContractError("PROMPT_EVAL_OUTPUT_UNSAFE")
    text = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--eval-root",
        type=Path,
        default=Path("evals/prompt_quality"),
    )
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)

    try:
        result = run_prompt_evals(args.eval_root)
        payload = normalize_prompt_eval_result(result)
    except PromptContractError as exc:
        payload = {
            "status": "BLOCKED",
            "error_code": exc.code,
        }
        if args.json_out is not None:
            try:
                _write_new_json(args.json_out, payload)
            except (OSError, PromptContractError):
                pass
        print(
            json.dumps(
                payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            )
        )
        return 2

    if args.json_out is not None:
        try:
            _write_new_json(args.json_out, payload)
        except (OSError, PromptContractError):
            print('{"error_code":"PROMPT_EVAL_OUTPUT_UNSAFE","status":"BLOCKED"}')
            return 2
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return 0 if result.status == "PASS" else (2 if result.status == "BLOCKED" else 1)


if __name__ == "__main__":
    raise SystemExit(run())
