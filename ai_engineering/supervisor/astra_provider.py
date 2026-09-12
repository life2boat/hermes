"""Real Astra Proposal Provider for Hermes Autonomous Supervisor (Task 7.7).

Astra is PROPOSAL-ONLY:
- It generates candidate NextActionProposals based on run state, budget, and task intent.
- It CANNOT grant authority, execute mutations, or sign policy receipts.
- Subprocess execution is bounded with strict termination (terminate -> wait -> kill).
- Validates proposal schema, run_id, task_id, and action types.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ai_engineering.contracts import EffectClass, StopBoundary
from ai_engineering.supervisor.autonomous_run import (
    AstraNextActionProposal,
    AstraProposalProvider,
    BudgetConfig,
    NextActionType,
)
from ai_engineering.supervisor.state import SupervisorState
from ai_engineering.task_intent import TaskIntent


class AstraProviderError(Exception):
    """Base error for Astra Proposal Provider."""
    code: str = "ASTRA_PROVIDER_ERROR"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class AstraProviderUnavailableError(AstraProviderError):
    code: str = "ASTRA_PROVIDER_UNAVAILABLE"


class AstraProviderTimeoutError(AstraProviderError):
    code: str = "ASTRA_PROVIDER_TIMEOUT"


class AstraProviderInvalidJsonError(AstraProviderError):
    code: str = "ASTRA_PROVIDER_INVALID_JSON"


class AstraProposalInvalidError(AstraProviderError):
    code: str = "ASTRA_PROPOSAL_INVALID"


class AstraProposalIdentityMismatchError(AstraProviderError):
    code: str = "ASTRA_PROPOSAL_IDENTITY_MISMATCH"


class AstraProposalActionUnsupportedError(AstraProviderError):
    code: str = "ASTRA_PROPOSAL_ACTION_UNSUPPORTED"


@dataclass(frozen=True, slots=True)
class AstraProviderReceipt:
    schema_version: str  # "hermes.astra-provider-receipt.v1"
    receipt_id: str
    run_id: str
    task_id: str
    proposal_id: str
    action_type: str
    cmd: tuple[str, ...]
    exit_code: int
    stdout_digest: str
    stderr_digest: str
    duration_ms: int
    created_at_utc: str


def compute_astra_receipt_digest(receipt_data: Mapping[str, Any]) -> str:
    canonical = json.dumps(receipt_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_canonical_astra_request(
    run_id: str,
    state: SupervisorState,
    intent: TaskIntent | None = None,
    budget: BudgetConfig | None = None,
    stats: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    b = budget or BudgetConfig()
    s = stats or {}

    iterations = s.get("iterations", 0)
    child_tasks = s.get("child_tasks", 0)
    retries = s.get("retries", 0)
    fix_cycles = s.get("fix_cycles", 0)
    consecutive_failures = s.get("consecutive_failures", 0)
    provider_calls = s.get("provider_calls", 0)

    task_intent_dict: dict[str, Any] = {}
    authority_boundary_dict: dict[str, Any] = {}

    if intent is not None:
        task_intent_dict = {
            "task_id": intent.task_id,
            "desired_outcome": intent.desired_outcome,
            "task_class": intent.task_class.value if hasattr(intent.task_class, "value") else str(intent.task_class),
            "stop_boundary": intent.stop_boundary.value if hasattr(intent.stop_boundary, "value") else str(intent.stop_boundary),
            "allowed_mutations": list(intent.allowed_mutations) if hasattr(intent, "allowed_mutations") else [],
            "forbidden_mutations": list(intent.forbidden_mutations) if hasattr(intent, "forbidden_mutations") else [],
            "required_gates": list(intent.required_gates) if hasattr(intent, "required_gates") else [],
            "parent_intent_digest": intent.parent_intent_digest,
        }
        authority_boundary_dict = {
            "allowed_scope": list(intent.allowed_mutations) if hasattr(intent, "allowed_mutations") else [],
            "stop_boundary": intent.stop_boundary.value if hasattr(intent.stop_boundary, "value") else str(intent.stop_boundary),
            "forbidden_mutations": list(intent.forbidden_mutations) if hasattr(intent, "forbidden_mutations") else [],
        }
    else:
        authority_boundary_dict = {
            "allowed_scope": [],
            "stop_boundary": "LOCAL_DIFF",
            "forbidden_mutations": [],
        }

    return {
        "schema_version": "hermes.astra-request.v1",
        "request_id": str(uuid.uuid4()),
        "run_id": run_id,
        "task_id": state.current_task_id,
        "attempt_id": state.current_attempt_id,
        "task_intent": task_intent_dict,
        "source": {
            "base_sha": state.current_base_sha,
            "current_intent_digest": state.current_intent_digest,
        },
        "supervisor": {
            "phase": getattr(state.phase, "value", str(state.phase)),
            "state_revision": state.state_revision,
            "iterations": iterations,
        },
        "latest_verified_result": {
            "result_id": state.latest_verified_result_id,
            "digest": state.latest_verified_result_digest,
        },
        "budget": {
            "max_supervisor_decisions": b.max_supervisor_decisions,
            "max_child_tasks": b.max_child_tasks,
            "max_retries": b.max_retries,
            "max_fix_cycles": b.max_fix_cycles,
            "max_consecutive_failures": b.max_consecutive_failures,
            "max_provider_calls": b.max_provider_calls,
            "consumed": {
                "iterations": iterations,
                "child_tasks": child_tasks,
                "retries": retries,
                "fix_cycles": fix_cycles,
                "consecutive_failures": consecutive_failures,
                "provider_calls": provider_calls,
            },
            "remaining": {
                "iterations": max(0, b.max_supervisor_decisions - iterations),
                "child_tasks": max(0, b.max_child_tasks - child_tasks),
                "retries": max(0, b.max_retries - retries),
                "fix_cycles": max(0, b.max_fix_cycles - fix_cycles),
                "consecutive_failures": max(0, b.max_consecutive_failures - consecutive_failures),
                "provider_calls": max(0, b.max_provider_calls - provider_calls),
            },
        },
        "authority_boundary": authority_boundary_dict,
        "context": {
            "blockers": list(state.blockers) if state.blockers else [],
        },
    }


def parse_and_validate_astra_proposal(
    data: Mapping[str, Any],
    expected_run_id: str,
    expected_task_id: str,
) -> AstraNextActionProposal:
    if not isinstance(data, dict):
        raise AstraProposalInvalidError("Proposal payload must be a JSON object")

    req_fields = (
        "schema_version",
        "proposal_id",
        "run_id",
        "task_id",
        "action_type",
        "objective",
        "recommended_capability",
        "recommended_worker",
        "expected_effect_class",
        "expected_stop_boundary",
    )
    for rf in req_fields:
        if rf not in data:
            raise AstraProposalInvalidError(f"Missing required proposal field: {rf}")

    # Check identity
    if data["run_id"] != expected_run_id:
        raise AstraProposalIdentityMismatchError(
            f"Proposal run_id '{data['run_id']}' != expected '{expected_run_id}'"
        )
    if data["task_id"] != expected_task_id:
        raise AstraProposalIdentityMismatchError(
            f"Proposal task_id '{data['task_id']}' != expected '{expected_task_id}'"
        )

    # Check action type
    raw_action = data["action_type"]
    valid_actions = {a.value for a in NextActionType}
    if raw_action not in valid_actions:
        raise AstraProposalActionUnsupportedError(
            f"Unsupported proposal action_type '{raw_action}'. Allowed: {sorted(valid_actions)}"
        )

    action_enum = NextActionType(raw_action)

    return AstraNextActionProposal(
        schema_version=str(data.get("schema_version", "hermes.astra-next-action.v1")),
        proposal_id=str(data["proposal_id"]),
        run_id=str(data["run_id"]),
        task_id=str(data["task_id"]),
        action_type=action_enum,
        objective=str(data.get("objective", "")),
        recommended_capability=str(data.get("recommended_capability", "")),
        recommended_worker=str(data.get("recommended_worker", "")),
        expected_effect_class=str(data.get("expected_effect_class", "")),
        expected_stop_boundary=str(data.get("expected_stop_boundary", "")),
        allowed_scope=tuple(data.get("allowed_scope", ())),
        required_validators=tuple(data.get("required_validators", ())),
        success_criteria=str(data.get("success_criteria", "")),
        reasoning_summary=str(data.get("reasoning_summary", "")),
    )


class ConfiguredAstraProposalProvider(AstraProposalProvider):
    """Subprocess-backed Astra Proposal Provider with bounded execution."""

    def __init__(
        self,
        cmd: Sequence[str],
        timeout_seconds: float = 30.0,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not cmd:
            raise ValueError("cmd cannot be empty for ConfiguredAstraProposalProvider")
        self.cmd = tuple(cmd)
        self.timeout_seconds = timeout_seconds
        self.cwd = cwd
        self.env = dict(env) if env is not None else None
        self.latest_receipt: AstraProviderReceipt | None = None

    async def request_proposal(
        self,
        run_id: str,
        state: SupervisorState,
        intent: TaskIntent | None = None,
        budget: BudgetConfig | None = None,
        stats: Mapping[str, int] | None = None,
        **kwargs: Any,
    ) -> AstraNextActionProposal:
        req_data = build_canonical_astra_request(
            run_id=run_id,
            state=state,
            intent=intent,
            budget=budget,
            stats=stats,
        )
        req_bytes = json.dumps(req_data, indent=2).encode("utf-8")

        start_time = time.monotonic()
        created_at_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=self.env,
            )
        except Exception as e:
            raise AstraProviderUnavailableError(
                f"Failed to spawn Astra command {self.cmd}: {e}"
            ) from e

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=req_bytes),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            # Terminate and cleanup process strictly (no orphan processes)
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    pass
            raise AstraProviderTimeoutError(
                f"Astra command timed out after {self.timeout_seconds}s"
            ) from e

        duration_ms = int((time.monotonic() - start_time) * 1000)
        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")
        stdout_digest = hashlib.sha256(stdout_bytes).hexdigest()
        stderr_digest = hashlib.sha256(stderr_bytes).hexdigest()

        if proc.returncode != 0:
            receipt_data = {
                "schema_version": "hermes.astra-provider-receipt.v1",
                "run_id": run_id,
                "task_id": state.current_task_id,
                "proposal_id": "",
                "action_type": "",
                "cmd": list(self.cmd),
                "exit_code": proc.returncode or -1,
                "stdout_digest": stdout_digest,
                "stderr_digest": stderr_digest,
                "duration_ms": duration_ms,
                "created_at_utc": created_at_utc,
            }
            receipt_id = compute_astra_receipt_digest(receipt_data)
            self.latest_receipt = AstraProviderReceipt(
                schema_version="hermes.astra-provider-receipt.v1",
                receipt_id=receipt_id,
                run_id=run_id,
                task_id=state.current_task_id,
                proposal_id="",
                action_type="",
                cmd=self.cmd,
                exit_code=proc.returncode or -1,
                stdout_digest=stdout_digest,
                stderr_digest=stderr_digest,
                duration_ms=duration_ms,
                created_at_utc=created_at_utc,
            )
            raise AstraProviderUnavailableError(
                f"Astra command {self.cmd[0]} exited with non-zero code {proc.returncode}: {stderr_str}"
            )

        try:
            parsed_json = json.loads(stdout_str)
        except json.JSONDecodeError as e:
            receipt_data = {
                "schema_version": "hermes.astra-provider-receipt.v1",
                "run_id": run_id,
                "task_id": state.current_task_id,
                "proposal_id": "",
                "action_type": "",
                "cmd": list(self.cmd),
                "exit_code": 0,
                "stdout_digest": stdout_digest,
                "stderr_digest": stderr_digest,
                "duration_ms": duration_ms,
                "created_at_utc": created_at_utc,
            }
            receipt_id = compute_astra_receipt_digest(receipt_data)
            self.latest_receipt = AstraProviderReceipt(
                schema_version="hermes.astra-provider-receipt.v1",
                receipt_id=receipt_id,
                run_id=run_id,
                task_id=state.current_task_id,
                proposal_id="",
                action_type="",
                cmd=self.cmd,
                exit_code=0,
                stdout_digest=stdout_digest,
                stderr_digest=stderr_digest,
                duration_ms=duration_ms,
                created_at_utc=created_at_utc,
            )
            raise AstraProviderInvalidJsonError(
                f"Astra output is not valid JSON: {e}"
            ) from e

        proposal = parse_and_validate_astra_proposal(
            parsed_json,
            expected_run_id=run_id,
            expected_task_id=state.current_task_id,
        )

        receipt_data = {
            "schema_version": "hermes.astra-provider-receipt.v1",
            "run_id": run_id,
            "task_id": state.current_task_id,
            "proposal_id": proposal.proposal_id,
            "action_type": proposal.action_type.value,
            "cmd": list(self.cmd),
            "exit_code": 0,
            "stdout_digest": stdout_digest,
            "stderr_digest": stderr_digest,
            "duration_ms": duration_ms,
            "created_at_utc": created_at_utc,
        }
        receipt_id = compute_astra_receipt_digest(receipt_data)
        self.latest_receipt = AstraProviderReceipt(
            schema_version="hermes.astra-provider-receipt.v1",
            receipt_id=receipt_id,
            run_id=run_id,
            task_id=state.current_task_id,
            proposal_id=proposal.proposal_id,
            action_type=proposal.action_type.value,
            cmd=self.cmd,
            exit_code=0,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            duration_ms=duration_ms,
            created_at_utc=created_at_utc,
        )

        return proposal
