#!/usr/bin/env python3
"""Evaluate sanitized procedure maturity evidence without graph execution."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from ai_engineering.contracts import Status
from ai_engineering.procedure_maturity import (
    PROCEDURE_MATURITY_SCHEMA_VERSION,
    ProcedureMaturityError,
    evaluate_procedure_maturity,
    normalize_procedure_maturity_receipt,
)


MAX_INPUT_BYTES = 1_048_576


class _DuplicateJsonKey(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    return parser


def _load_input(path: Path) -> dict[str, object]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ProcedureMaturityError("PROCEDURE_MATURITY_INPUT_UNSAFE")
        raw = path.read_bytes()
        if not raw or len(raw) > MAX_INPUT_BYTES or b"\x00" in raw:
            raise ProcedureMaturityError("PROCEDURE_MATURITY_INPUT_UNSAFE")
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except ProcedureMaturityError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProcedureMaturityError("PROCEDURE_MATURITY_INPUT_UNSAFE") from exc
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise ProcedureMaturityError("PROCEDURE_MATURITY_INPUT_UNSAFE")
    return payload


def _blocked_payload(code: str) -> dict[str, object]:
    return {
        "schema_version": PROCEDURE_MATURITY_SCHEMA_VERSION,
        "status": Status.BLOCKED.value,
        "reason_codes": [code],
    }


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = evaluate_procedure_maturity(_load_input(args.input))
        payload = normalize_procedure_maturity_receipt(receipt)
        exit_code = {
            Status.PASS: 0,
            Status.FAIL: 1,
            Status.BLOCKED: 2,
        }[receipt.graph_candidate_eligible]
    except ProcedureMaturityError as exc:
        payload = _blocked_payload(exc.code)
        exit_code = 2
    except Exception:
        payload = _blocked_payload("PROCEDURE_MATURITY_INTERNAL_ERROR")
        exit_code = 3
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
