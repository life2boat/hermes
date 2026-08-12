#!/usr/bin/env python3
"""Build one sanitized, review-only Failure-to-Eval candidate offline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from ai_engineering.contracts import Status
from ai_engineering.failure_candidate import (
    FAILURE_CANDIDATE_SCHEMA_VERSION,
    MAX_FAILURE_EVIDENCE_BYTES,
    FailureCandidateError,
    FailureCandidatePolicyError,
    build_failure_eval_candidate,
    make_failure_candidate_receipt,
    normalize_failure_candidate_receipt,
    write_failure_eval_candidate,
)


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
    parser.add_argument("--repository-root", type=Path, default=_REPOSITORY_ROOT)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _load_input(path: Path) -> dict[str, object]:
    try:
        if path.is_symlink() or not path.is_file():
            raise FailureCandidateError("FAILURE_CANDIDATE_INPUT_UNSAFE")
        raw = path.read_bytes()
        if not raw or len(raw) > MAX_FAILURE_EVIDENCE_BYTES or b"\x00" in raw:
            raise FailureCandidateError("FAILURE_CANDIDATE_INPUT_UNSAFE")
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except FailureCandidateError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise FailureCandidateError("FAILURE_CANDIDATE_INPUT_UNSAFE") from exc
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise FailureCandidateError("FAILURE_CANDIDATE_INPUT_UNSAFE")
    return payload


def _failure_payload(status: Status, code: str) -> dict[str, object]:
    return {
        "schema_version": FAILURE_CANDIDATE_SCHEMA_VERSION,
        "status": status.value,
        "reason_codes": [code],
    }


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.dry_run and args.output is None:
        payload = _failure_payload(Status.BLOCKED, "FAILURE_CANDIDATE_OUTPUT_REQUIRED")
        exit_code = 2
    else:
        try:
            candidate = build_failure_eval_candidate(
                args.repository_root,
                _load_input(args.input),
            )
            if not args.dry_run:
                write_failure_eval_candidate(args.repository_root, args.output, candidate)
            payload = normalize_failure_candidate_receipt(make_failure_candidate_receipt(candidate))
            exit_code = 0
        except FailureCandidatePolicyError as exc:
            payload = _failure_payload(Status.FAIL, exc.code)
            exit_code = 1
        except FailureCandidateError as exc:
            payload = _failure_payload(Status.BLOCKED, exc.code)
            exit_code = 2
        except Exception:
            payload = _failure_payload(Status.BLOCKED, "FAILURE_CANDIDATE_INTERNAL_ERROR")
            exit_code = 3
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
