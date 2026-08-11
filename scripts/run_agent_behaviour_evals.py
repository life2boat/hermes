#!/usr/bin/env python3
"""Run the committed offline Hermes behaviour-evaluation corpus."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ai_engineering.contracts import BEHAVIOUR_EVAL_ENGINE_VERSION, Status
from ai_engineering.eval_runner import (
    EvalConfigurationError,
    normalize_eval_result,
    run_evals,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--category")
    parser.add_argument("--case")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--no-baseline", action="store_true")
    return parser


def _write_new_json(path: Path, payload: dict[str, object]) -> None:
    if path.is_symlink() or path.exists():
        raise EvalConfigurationError("EVAL_OUTPUT_UNSAFE")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise EvalConfigurationError("EVAL_OUTPUT_UNSAFE")
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
    except OSError as exc:
        raise EvalConfigurationError("EVAL_OUTPUT_UNSAFE") from exc


def _blocked_payload(code: str) -> dict[str, object]:
    return {
        "engine_version": BEHAVIOUR_EVAL_ENGINE_VERSION,
        "status": Status.BLOCKED.value,
        "reason_code": code,
    }


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_evals(
            _REPO_ROOT / "evals" / "agent_behaviour",
            smoke=args.smoke,
            category=args.category,
            case_id=args.case,
            use_baseline=not args.no_baseline,
        )
        payload = normalize_eval_result(result)
        exit_code = {
            Status.PASS: 0,
            Status.FAIL: 1,
            Status.BLOCKED: 2,
        }[result.status]
    except EvalConfigurationError as exc:
        payload = _blocked_payload(exc.code)
        exit_code = 2
    except Exception:
        payload = _blocked_payload("EVAL_INTERNAL_ERROR")
        exit_code = 3
    if args.json_out is not None:
        try:
            _write_new_json(args.json_out, payload)
        except EvalConfigurationError as exc:
            payload = _blocked_payload(exc.code)
            exit_code = 2
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
