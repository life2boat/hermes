#!/usr/bin/env python3
"""Build a sanitized, repository-bound context package for an AI task."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure repository root is on sys.path when script is executed directly
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ai_engineering.task_intent import (
    TaskIntentValidationError,
    deserialize_intent,
    normalize_intent,
)

SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
CORE_DOCUMENTS = (
    "docs/HERMES_SOURCE_MAP.md",
    "docs/HERMES_SYSTEM_MODEL.md",
    "docs/HERMES_INVARIANTS.md",
    "docs/AI_AGENT_RULEBOOK.md",
    "docs/TASK_TEMPLATE.md",
    "docs/CURRENT_STATE.md",
    "docs/AGENT_BEHAVIOUR_CONTRACT.md",
    "docs/BEHAVIOUR_EVALS.md",
    "docs/contracts/PROMPT_DESIGN_CONTRACT.md",
    "docs/contracts/PROMPT_EVAL_CONTRACT.md",
    "docs/contracts/PROMPT_FAILURE_TAXONOMY.md",
    "docs/PROMPT_AUTHORING_GUIDE.md",
    "docs/LLM_OPS_POLICY.md",
    "docs/AGENT_RELEASE_GATES.md",
    "docs/SKILL_LOOP_GRAPH_LIFECYCLE.md",
)
CANONICAL_MAIN_REF = "refs/remotes/github/main"


class PrepareTaskError(RuntimeError):
    """A sanitized, stable failure class for task-context preparation."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _run_git(
    repository_root: Path,
    *args: str,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repository_root,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise PrepareTaskError("GIT_COMMAND_UNAVAILABLE") from exc
    if result.returncode and not allow_failure:
        raise PrepareTaskError("GIT_COMMAND_FAILED")
    return result


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="replace")


def _repository_root(candidate: Path) -> Path:
    if candidate.is_symlink():
        raise PrepareTaskError("REPOSITORY_ROOT_UNSAFE")
    try:
        root = candidate.resolve()
    except OSError as exc:
        raise PrepareTaskError("REPOSITORY_ROOT_UNSAFE") from exc
    if not root.is_dir():
        raise PrepareTaskError("REPOSITORY_ROOT_UNSAFE")
    result = _run_git(root, "rev-parse", "--show-toplevel")
    discovered = Path(_decode(result.stdout).strip()).resolve()
    if discovered != root:
        raise PrepareTaskError("REPOSITORY_ROOT_MISMATCH")
    return root


def _parse_porcelain(raw: bytes) -> list[dict[str, str]]:
    records = raw.split(b"\0")
    changes: list[dict[str, str]] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        text = _decode(record)
        if len(text) < 4 or text[2] != " ":
            raise PrepareTaskError("GIT_STATUS_UNPARSABLE")
        status = text[:2]
        item = {"status": status, "path": text[3:]}
        if "R" in status or "C" in status:
            if index >= len(records) or not records[index]:
                raise PrepareTaskError("GIT_STATUS_UNPARSABLE")
            item["original_path"] = _decode(records[index])
            index += 1
        changes.append(item)
    return changes


def _tracked_adrs(repository_root: Path) -> list[str]:
    result = _run_git(
        repository_root,
        "ls-files",
        "-z",
        "--",
        "docs/adr",
    )
    paths = [
        _decode(item)
        for item in result.stdout.split(b"\0")
        if item and _decode(item).endswith(".md")
    ]
    return sorted(paths)


def _document(repository_root: Path, relative_path: str) -> dict[str, Any]:
    candidate = repository_root / relative_path
    if candidate.is_symlink() or not candidate.is_file():
        raise PrepareTaskError("REQUIRED_DOCUMENT_UNSAFE_OR_MISSING")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(repository_root):
        raise PrepareTaskError("REQUIRED_DOCUMENT_OUTSIDE_REPOSITORY")
    try:
        raw = candidate.read_bytes()
    except OSError as exc:
        raise PrepareTaskError("REQUIRED_DOCUMENT_UNREADABLE") from exc
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise PrepareTaskError("REQUIRED_DOCUMENT_TOO_LARGE")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrepareTaskError("REQUIRED_DOCUMENT_NOT_UTF8") from exc
    return {
        "path": relative_path.replace("\\", "/"),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "content": content,
    }


def _pytest_evidence(repository_root: Path) -> dict[str, Any]:
    cache_path = repository_root / ".pytest_cache" / "v" / "cache" / "lastfailed"
    if not cache_path.exists():
        return {
            "classification": "NOT_AVAILABLE",
            "source": ".pytest_cache/v/cache/lastfailed",
            "reason": "NO_PYTEST_OUTCOME_RECEIPT_OR_LASTFAILED_CACHE",
            "pass_claim_allowed": False,
        }
    if cache_path.is_symlink() or not cache_path.is_file():
        return {
            "classification": "INCONCLUSIVE",
            "source": ".pytest_cache/v/cache/lastfailed",
            "reason": "PYTEST_CACHE_PATH_UNSAFE",
            "pass_claim_allowed": False,
        }
    try:
        raw = cache_path.read_bytes()
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {
            "classification": "INCONCLUSIVE",
            "source": ".pytest_cache/v/cache/lastfailed",
            "reason": "PYTEST_CACHE_UNREADABLE",
            "pass_claim_allowed": False,
        }
    failed_count = len(payload)
    observed = datetime.fromtimestamp(
        cache_path.stat().st_mtime,
        tz=timezone.utc,
    ).isoformat()
    return {
        "classification": "INCONCLUSIVE",
        "source": ".pytest_cache/v/cache/lastfailed",
        "cache_state": (
            "RECORDED_FAILURES" if failed_count else "NO_RECORDED_FAILURES"
        ),
        "failed_test_count": failed_count,
        "observed_mtime_utc": observed,
        "reason": "PYTEST_CACHE_IS_NOT_A_COMMIT_BOUND_LAST_RUN_RECEIPT",
        "pass_claim_allowed": False,
    }


def build_task_context(
    repository_root: Path,
    intent_path: Path | None = None,
) -> dict[str, Any]:
    root = _repository_root(repository_root)
    head_sha = _decode(_run_git(root, "rev-parse", "HEAD").stdout).strip()
    branch = _decode(_run_git(root, "branch", "--show-current").stdout).strip()
    status_raw = _run_git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ).stdout
    changed_files = _parse_porcelain(status_raw)
    canonical = _run_git(
        root,
        "rev-parse",
        "--verify",
        CANONICAL_MAIN_REF,
        allow_failure=True,
    )
    canonical_sha = (
        _decode(canonical.stdout).strip() if canonical.returncode == 0 else None
    )

    intent_payload = None
    if intent_path is not None:
        if intent_path.is_symlink() or not intent_path.is_file():
            raise PrepareTaskError("INTENT_FILE_UNSAFE_OR_MISSING")
        try:
            intent_raw = intent_path.read_bytes()
        except OSError as exc:
            raise PrepareTaskError("INTENT_FILE_UNREADABLE") from exc
        try:
            intent = deserialize_intent(intent_raw)
        except TaskIntentValidationError as exc:
            raise PrepareTaskError(f"INTENT_VALIDATION_FAILED_{exc.code}") from exc
        if intent.source_base_sha != head_sha:
            raise PrepareTaskError("INTENT_BASE_SHA_MISMATCH")
        intent_payload = normalize_intent(intent)

    document_paths = [*CORE_DOCUMENTS, *_tracked_adrs(root)]
    documents = [_document(root, path) for path in document_paths]
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": {
            "head_sha": head_sha,
            "branch": branch or "DETACHED",
            "canonical_main_ref": CANONICAL_MAIN_REF,
            "canonical_main_sha": canonical_sha,
            "worktree_clean": not changed_files,
        },
        "changed_files": changed_files,
        "test_evidence": _pytest_evidence(root),
        "documents": documents,
    }
    if intent_payload is not None:
        payload["intent"] = intent_payload
    return payload


def _output_path(repository_root: Path, requested: Path) -> Path:
    context_root = (repository_root / ".task_context").resolve()
    output = (
        requested.resolve()
        if requested.is_absolute()
        else (repository_root / requested).resolve()
    )
    if not output.is_relative_to(context_root) or output == context_root:
        raise PrepareTaskError("OUTPUT_OUTSIDE_TASK_CONTEXT")
    current = repository_root
    for part in output.relative_to(repository_root).parts[:-1]:
        current /= part
        if current.exists() and current.is_symlink():
            raise PrepareTaskError("OUTPUT_PARENT_SYMLINK")
    if output.exists() and (output.is_symlink() or not output.is_file()):
        raise PrepareTaskError("OUTPUT_PATH_UNSAFE")
    return output


def _write_context(
    repository_root: Path, requested: Path, payload: dict[str, Any]
) -> str:
    output = _output_path(repository_root, requested)
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="x",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)
    return output.relative_to(repository_root).as_posix()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a sanitized, repository-bound AI task context.",
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Exact Git worktree root (defaults to this script's repository).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSON under .task_context/; otherwise emit JSON to stdout.",
    )
    parser.add_argument(
        "--intent",
        type=Path,
        help="Path to an optional TaskIntent JSON file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = _repository_root(args.repository_root)
        payload = build_task_context(root, args.intent)
        if args.output is None:
            json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
            sys.stdout.write("\n")
        else:
            relative = _write_context(root, args.output, payload)
            print(f"TASK_CONTEXT_WRITTEN={relative}")
        return 0
    except PrepareTaskError as exc:
        print(f"prepare_task: {exc.code}", file=sys.stderr)
        return 2
    except OSError:
        print("prepare_task: FILESYSTEM_OPERATION_FAILED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
