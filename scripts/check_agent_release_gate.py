#!/usr/bin/env python3
"""Evaluate Hermes release evidence or orchestrate the PR merge gate."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ai_engineering.contracts import Status
from ai_engineering.release_gate import (
    GOLDEN_CORPUS_DIGEST,
    RELEASE_GATE_POLICY_VERSION,
    RELEASE_GATE_SCHEMA_VERSION,
    BlockerScope,
    GateEvidence,
    GateName,
    ReleaseGateError,
    ReleaseTarget,
    ReleaseTaskClassification,
    SourceIdentity,
    TechnicalBlocker,
    derive_gate_requirements,
    evaluate_release,
    evaluate_release_mapping,
    load_release_request,
    normalize_release_receipt,
)


_PROVIDER_ENV = (
    "ANTHROPIC_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "NOUS_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "QWEN_API_KEY",
    "TELEGRAM_BOT_TOKEN",
)
_SECRET_RANGE_ENV = (
    "HERMES_SECRET_CHECK_BASE_SHA",
    "HERMES_SECRET_CHECK_SOURCE_SHA",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)

    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--input", required=True)
    evaluate.add_argument("--root", type=Path, default=_REPO_ROOT)
    evaluate.add_argument("--json-out", type=Path)

    ci_merge = commands.add_parser("ci-merge")
    ci_merge.add_argument("--base-sha", required=True)
    ci_merge.add_argument("--candidate-sha", required=True)
    ci_merge.add_argument("--repository", default="life2boat/hermes")
    ci_merge.add_argument("--canonical-remote", default="github")
    ci_merge.add_argument("--task-id", default="agent-release-gate-ci")
    ci_merge.add_argument("--json-out", type=Path, required=True)
    return parser


def _root(value: Path) -> Path:
    try:
        root = value.resolve(strict=True)
    except OSError as exc:
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID") from exc
    if not root.is_dir() or root.is_symlink():
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID")
    return root


def _write_new_json(path: Path, payload: dict[str, object]) -> None:
    if path.is_symlink() or path.exists():
        raise ReleaseGateError("RELEASE_GATE_OUTPUT_UNSAFE")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise ReleaseGateError("RELEASE_GATE_OUTPUT_UNSAFE") from exc
    if not parent.is_dir():
        raise ReleaseGateError("RELEASE_GATE_OUTPUT_UNSAFE")
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
        raise ReleaseGateError("RELEASE_GATE_OUTPUT_UNSAFE") from exc


def _blocked_payload(code: str, mode: str | None) -> dict[str, object]:
    return {
        "mode": mode,
        "policy_version": RELEASE_GATE_POLICY_VERSION,
        "reason_codes": [code],
        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
        "status": Status.BLOCKED.value,
    }


def _exit_code(status: Status) -> int:
    return {Status.PASS: 0, Status.FAIL: 1, Status.BLOCKED: 2}[status]


def _command_status(returncode: int) -> Status:
    if returncode == 0:
        return Status.PASS
    if returncode == 1:
        return Status.FAIL
    return Status.BLOCKED


def _safe_environment() -> dict[str, str]:
    env = dict(os.environ)
    for name in (*_PROVIDER_ENV, *_SECRET_RANGE_ENV):
        env[name] = ""
    env["TZ"] = "UTC"
    env["LANG"] = "C.UTF-8"
    return env


def _run_command(
    command: list[str],
    *,
    env: dict[str, str],
    quiet: bool = False,
) -> int:
    result = subprocess.run(
        command,
        cwd=_REPO_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.DEVNULL if quiet else None,
        check=False,
    )
    return result.returncode


def _load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseGateError("RELEASE_GATE_CI_EVIDENCE_INVALID") from exc
    if not isinstance(value, dict):
        raise ReleaseGateError("RELEASE_GATE_CI_EVIDENCE_INVALID")
    return value


def _behaviour_status(value: dict[str, object], *, full: bool) -> Status:
    try:
        status = Status(value["status"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReleaseGateError("RELEASE_GATE_CI_EVIDENCE_INVALID") from exc
    if status is not Status.PASS:
        return status if status in {Status.FAIL, Status.BLOCKED} else Status.BLOCKED
    if not full:
        return Status.PASS
    identity_ok = (
        value.get("baseline_status") == Status.PASS.value
        and value.get("corpus_digest") == GOLDEN_CORPUS_DIGEST
        and value.get("critical_failed") == 0
        and isinstance(value.get("critical_total"), int)
        and value.get("critical_total") == value.get("critical_passed")
        and value.get("critical_total", 0) > 0
    )
    return Status.PASS if identity_ok else Status.BLOCKED


def _manifest_is_golden() -> bool:
    try:
        manifest = json.loads(
            (_REPO_ROOT / "evals" / "agent_behaviour" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(manifest, dict) and manifest.get("corpus_status") == "GOLDEN"


def _git_output(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseGateError("RELEASE_GATE_SOURCE_IDENTITY_MISSING") from exc
    return completed.stdout.strip()


def _ci_merge(args: argparse.Namespace):
    observed_head = _git_output("rev-parse", "HEAD")
    source = SourceIdentity(
        repository=args.repository,
        canonical_remote=args.canonical_remote,
        base_sha=args.base_sha,
        candidate_sha=args.candidate_sha,
        observed_head_sha=observed_head,
        task_id=args.task_id,
    )
    classification = ReleaseTaskClassification(
        task_classification="CONSERVATIVE_PR_MERGE",
        behaviour_sensitive=True,
        security_sensitive=True,
        cost_sensitive=False,
        production_sensitive=False,
        live_behaviour_required=False,
    )
    requirements = derive_gate_requirements(ReleaseTarget.MERGE, classification)
    blockers: list[TechnicalBlocker] = []

    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", args.base_sha, args.candidate_sha],
        cwd=_REPO_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode
    if ancestry != 0:
        blockers.append(
            TechnicalBlocker(
                code=(
                    "BASE_NOT_ANCESTOR"
                    if ancestry == 1
                    else "ANCESTRY_EVIDENCE_UNAVAILABLE"
                ),
                status=Status.FAIL if ancestry == 1 else Status.BLOCKED,
                evidence_refs=("source:git_ancestry",),
                scope=BlockerScope.MERGE,
            )
        )

    code_status = Status.NOT_RUN
    behaviour_status = Status.NOT_RUN
    security_status = Status.NOT_RUN
    safe_to_run = observed_head == args.candidate_sha and ancestry == 0
    if safe_to_run:
        env = _safe_environment()
        code_status = _command_status(
            _run_command(["bash", "scripts/agent_check.sh"], env=env)
        )
        with tempfile.TemporaryDirectory(prefix="hermes-release-gate-") as temporary:
            temp_root = Path(temporary)
            behaviour_path = temp_root / "behaviour.json"
            behaviour_rc = _run_command(
                [
                    sys.executable,
                    "scripts/run_agent_behaviour_evals.py",
                    "--json-out",
                    str(behaviour_path),
                ],
                env=env,
                quiet=True,
            )
            if behaviour_path.is_file():
                behaviour_status = _behaviour_status(
                    _load_json(behaviour_path), full=True
                )
            else:
                behaviour_status = _command_status(behaviour_rc)
            if behaviour_status is Status.PASS and not _manifest_is_golden():
                behaviour_status = Status.BLOCKED

            secret_env = dict(env)
            secret_env["HERMES_SECRET_CHECK_BASE_SHA"] = args.base_sha
            secret_env["HERMES_SECRET_CHECK_SOURCE_SHA"] = args.candidate_sha
            secret_status = _command_status(
                _run_command(
                    ["bash", "scripts/secret_check.sh"], env=secret_env
                )
            )
            adversarial_path = temp_root / "adversarial.json"
            adversarial_rc = _run_command(
                [
                    sys.executable,
                    "scripts/run_agent_behaviour_evals.py",
                    "--category",
                    "adversarial",
                    "--json-out",
                    str(adversarial_path),
                ],
                env=env,
                quiet=True,
            )
            if adversarial_path.is_file():
                adversarial_status = _behaviour_status(
                    _load_json(adversarial_path), full=False
                )
            else:
                adversarial_status = _command_status(adversarial_rc)
            security_status = (
                Status.FAIL
                if Status.FAIL in {secret_status, adversarial_status}
                else Status.BLOCKED
                if Status.BLOCKED in {secret_status, adversarial_status}
                else Status.PASS
            )

    statuses = {
        GateName.CODE: code_status,
        GateName.BEHAVIOUR: behaviour_status,
        GateName.SECURITY: security_status,
        GateName.LIVE_BEHAVIOUR: Status.NOT_PERFORMED,
        GateName.COST: Status.NOT_PERFORMED,
        GateName.PRODUCTION_READINESS: Status.NOT_PERFORMED,
    }
    refs = {
        GateName.CODE: ("code:agent_check",),
        GateName.BEHAVIOUR: ("behaviour:golden_corpus",),
        GateName.SECURITY: (
            "behaviour:adversarial",
            "security:secret_check",
        ),
        GateName.LIVE_BEHAVIOUR: (),
        GateName.COST: (),
        GateName.PRODUCTION_READINESS: (),
    }
    gates = tuple(
        GateEvidence(
            gate_name=name,
            required=requirements[name],
            status=statuses[name],
            evidence_refs=refs[name],
            reason_codes=(),
            evidence_digest=(
                GOLDEN_CORPUS_DIGEST if name is GateName.BEHAVIOUR else None
            ),
        )
        for name in GateName
    )
    return evaluate_release(
        target=ReleaseTarget.MERGE,
        source=source,
        classification=classification,
        gate_results=gates,
        technical_blockers=blockers,
        governance_observations=(),
    )


def _print_summary(payload: dict[str, object]) -> None:
    gates = payload.get("gate_results")
    if not isinstance(gates, list):
        return
    for item in gates:
        if isinstance(item, dict):
            print(f"{item.get('gate_name')}={item.get('status')}")
    print(f"MERGE_ELIGIBLE={payload.get('merge_eligible')}")
    print(
        "PRODUCTION_RELEASE_ELIGIBLE="
        f"{payload.get('production_release_eligible')}"
    )
    print(f"CANDIDATE_SHA={payload.get('candidate_sha')}")


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "evaluate":
            root = _root(args.root)
            receipt = evaluate_release_mapping(load_release_request(root, args.input))
        else:
            receipt = _ci_merge(args)
        payload = normalize_release_receipt(receipt)
        exit_code = _exit_code(receipt.status)
    except ReleaseGateError as exc:
        payload = _blocked_payload(exc.code, getattr(args, "mode", None))
        exit_code = 2
    except Exception:
        payload = _blocked_payload(
            "RELEASE_GATE_INTERNAL_ERROR", getattr(args, "mode", None)
        )
        exit_code = 3

    if args.json_out is not None:
        try:
            _write_new_json(args.json_out, payload)
        except ReleaseGateError as exc:
            payload = _blocked_payload(exc.code, getattr(args, "mode", None))
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
    _print_summary(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
