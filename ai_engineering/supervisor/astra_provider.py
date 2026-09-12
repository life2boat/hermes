"""Real Astra Proposal Provider for Hermes Autonomous Supervisor (Task 7.7 Corrective Closure).

Astra is PROPOSAL-ONLY:
- It generates candidate NextActionProposals based on run state, budget, and task intent.
- It CANNOT grant authority, execute mutations, or sign policy receipts.
- Subprocess execution is bounded with strict termination (terminate -> wait -> kill).
- Validates exact proposal schema (hermes.astra-next-action.v1), identity (run_id, task_id), action types, and EffectClass/StopBoundary enums.
- Generates redacted, secret-safe receipts (provider_executable, command_digest, argument_count).
- Produces failure receipts for all provider failure paths.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ai_engineering.contracts import EffectClass, StopBoundary
from ai_engineering.control_plane.orchestrator import _STOP_BOUNDARY_RANK
from ai_engineering.effective_policy import EffectivePolicyReport
from ai_engineering.supervisor.autonomous_run import (
    AstraNextActionProposal,
    AstraProposalProvider,
    BudgetConfig,
    NextActionType,
)
from ai_engineering.supervisor.policy.contracts import WorkProfile
from ai_engineering.supervisor.state import SupervisorState
from ai_engineering.supervisor.validator import VerifiedResult
from ai_engineering.task_intent import TaskIntent, intent_digest


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
    provider_executable: str
    command_digest: str
    argument_count: int
    exit_code: int
    stdout_digest: str
    stderr_digest: str
    duration_ms: int
    created_at_utc: str

    @property
    def cmd(self) -> tuple[str, ...]:
        """Backward compatibility property returning provider executable and summary."""
        return (self.provider_executable, f"args:{self.argument_count}", f"digest:{self.command_digest[:8]}")


def compute_command_digest(cmd: Sequence[str]) -> str:
    canonical = " ".join(cmd)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_astra_receipt_digest(receipt_data: Mapping[str, Any]) -> str:
    canonical = json.dumps(receipt_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _most_restrictive_stop_boundary(*boundaries: Any) -> str:
    best_rank = 999
    best_boundary = "READ_ONLY"
    for b in boundaries:
        if not b:
            continue
        val = b.value if hasattr(b, "value") else str(b)
        try:
            sb = StopBoundary(val)
            rank = _STOP_BOUNDARY_RANK.get(sb, 999)
        except Exception:
            rank = 999
        if rank < best_rank:
            best_rank = rank
            best_boundary = val
    return best_boundary


def build_canonical_astra_request(
    run_id: str,
    state: SupervisorState,
    intent: TaskIntent | None = None,
    budget: BudgetConfig | None = None,
    stats: Mapping[str, int] | None = None,
    work_profile: WorkProfile | None = None,
    effective_policy: EffectivePolicyReport | None = None,
    verified_result: VerifiedResult | None = None,
    context_pack_digest: str | None = None,
    real_mode: bool = False,
) -> dict[str, Any]:
    """Builds canonical hermes.astra-request.v1 payload conforming to Section 15 contract."""
    if real_mode:
        if work_profile is None or effective_policy is None or intent is None:
            raise AstraProviderUnavailableError(
                "ASTRA_AUTHORITY_CONTEXT_UNAVAILABLE: WorkProfile, EffectivePolicy, and TaskIntent must be present in real mode",
                code="ASTRA_AUTHORITY_CONTEXT_UNAVAILABLE",
            )

    b = budget or BudgetConfig()
    s = stats or {}

    iterations = s.get("iterations", 0)
    child_tasks = s.get("child_tasks", 0)
    retries = s.get("retries", 0)
    fix_cycles = s.get("fix_cycles", 0)
    consecutive_failures = s.get("consecutive_failures", 0)
    provider_calls = s.get("provider_calls", 0)

    # 1. Task Intent details
    task_intent_id = intent.task_id if intent else state.current_task_id
    task_intent_dg = intent_digest(intent) if intent else state.current_intent_digest
    desired_outcome = intent.desired_outcome if intent else ""
    task_class = (
        intent.task_class.value
        if (intent and hasattr(intent.task_class, "value"))
        else (str(intent.task_class) if intent else "")
    )

    # 2. Source details
    repository = getattr(state, "repository", None) or (intent.source_repository if intent else "life2boat/hermes")
    canonical_remote = getattr(state, "canonical_remote", None) or "github"
    base_sha = state.current_base_sha

    # 3. Supervisor details
    phase_val = state.phase.value if hasattr(state.phase, "value") else str(state.phase)
    blockers_list = list(state.blockers) if state.blockers else []

    # 4. Latest Verified Result details
    vr_status = ""
    gate_results_list = []
    if verified_result:
        vr_status = verified_result.status.value if hasattr(verified_result.status, "value") else str(verified_result.status)
        for gr in verified_result.gate_results:
            gate_results_list.append({
                "gate_name": gr.gate_name,
                "status": gr.status.value if hasattr(gr.status, "value") else str(gr.status),
                "required": gr.required,
                "reason_code": getattr(gr, "reason_code", ""),
            })

    # 5. Authority Boundary derived from conservative intersection of TaskIntent + WorkProfile + EffectivePolicy.task_policy
    # Forbidden mutations (UNION of all sources)
    forbidden_set: set[str] = set()
    if intent and hasattr(intent, "forbidden_mutations") and intent.forbidden_mutations:
        forbidden_set.update(intent.forbidden_mutations)
    if effective_policy and hasattr(effective_policy, "task_policy") and effective_policy.task_policy:
        if hasattr(effective_policy.task_policy, "forbidden_mutations") and effective_policy.task_policy.forbidden_mutations:
            forbidden_set.update(effective_policy.task_policy.forbidden_mutations)

    # Allowed mutations (INTERSECTION of intent and effective policy, minus forbidden)
    intent_allowed = list(intent.allowed_mutations) if (intent and hasattr(intent, "allowed_mutations")) else []
    if effective_policy and hasattr(effective_policy, "task_policy") and effective_policy.task_policy:
        ep_allowed = set(effective_policy.task_policy.allowed_mutations) if hasattr(effective_policy.task_policy, "allowed_mutations") else set()
        if ep_allowed:
            allowed_mutations = [m for m in intent_allowed if m in ep_allowed]
        else:
            allowed_mutations = intent_allowed
    else:
        allowed_mutations = intent_allowed

    # Astra must never see forbidden scope as allowed!
    allowed_mutations = [m for m in allowed_mutations if m not in forbidden_set]

    # Stop boundary: most restrictive trusted boundary
    boundaries = []
    if intent and hasattr(intent, "stop_boundary"):
        boundaries.append(intent.stop_boundary)
    if effective_policy and hasattr(effective_policy, "task_policy") and effective_policy.task_policy:
        if hasattr(effective_policy.task_policy, "stop_boundary"):
            boundaries.append(effective_policy.task_policy.stop_boundary)
    stop_b = _most_restrictive_stop_boundary(*boundaries) if boundaries else "LOCAL_DIFF"

    # Effect classes: trusted WorkProfile restrictions plus any applicable effective-policy constraints
    if work_profile:
        allowed_effects = [e.value if hasattr(e, "value") else str(e) for e in work_profile.allowed_effect_classes]
        forbidden_effects = set(e.value if hasattr(e, "value") else str(e) for e in work_profile.forbidden_effect_classes)
        production_allowed = work_profile.production_execution_allowed
    elif not real_mode:
        allowed_effects = ["READ_ONLY", "REPOSITORY_WRITE"]
        forbidden_effects = set()
        production_allowed = False
    else:
        allowed_effects = ["READ_ONLY"]
        forbidden_effects = set()
        production_allowed = False

    # Apply effective policy forbidden mutations that correspond to effect classes
    for fm in forbidden_set:
        if fm in allowed_effects:
            forbidden_effects.add(fm)

    allowed_effects = [e for e in allowed_effects if e not in forbidden_effects]
    forbidden_effects_list = sorted(list(forbidden_effects))
    forbidden_mutations_list = sorted(list(forbidden_set))

    return {
        "schema_version": "hermes.astra-request.v1",
        "request_id": str(uuid.uuid4()),
        "run_id": run_id,
        "task_id": state.current_task_id,
        "attempt_id": state.current_attempt_id,
        "task_intent": {
            "task_intent_id": task_intent_id,
            "task_intent_digest": task_intent_dg,
            "desired_outcome": desired_outcome,
            "task_class": task_class,
        },
        "source": {
            "repository": repository,
            "canonical_remote": canonical_remote,
            "base_sha": base_sha,
        },
        "supervisor": {
            "phase": phase_val,
            "state_revision": state.state_revision,
            "blockers": blockers_list,
            "iterations": iterations,
        },
        "latest_verified_result": {
            "id": state.latest_verified_result_id,
            "digest": state.latest_verified_result_digest,
            "status": vr_status,
            "required_gate_results": gate_results_list,
        },
        "budget": {
            "max_supervisor_decisions": b.max_supervisor_decisions,
            "max_child_tasks": b.max_child_tasks,
            "max_retries": b.max_retries,
            "max_fix_cycles": b.max_fix_cycles,
            "max_consecutive_failures": b.max_consecutive_failures,
            "max_provider_calls": b.max_provider_calls,
            "maximum": {
                "max_supervisor_decisions": b.max_supervisor_decisions,
                "max_child_tasks": b.max_child_tasks,
                "max_retries": b.max_retries,
                "max_fix_cycles": b.max_fix_cycles,
                "max_consecutive_failures": b.max_consecutive_failures,
                "max_provider_calls": b.max_provider_calls,
            },
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
        "authority_boundary": {
            "allowed_capabilities": allowed_mutations,
            "allowed_mutations": allowed_mutations,
            "forbidden_mutations": forbidden_mutations_list,
            "allowed_effect_classes": allowed_effects,
            "forbidden_effect_classes": forbidden_effects_list,
            "stop_boundary": stop_b,
            "production_execution_allowed": production_allowed,
        },
        "context": {
            "context_pack_digest": context_pack_digest or "",
        },
    }


def parse_and_validate_astra_proposal(
    data: Mapping[str, Any],
    expected_run_id: str,
    expected_task_id: str,
) -> AstraNextActionProposal:
    if not isinstance(data, dict):
        raise AstraProposalInvalidError("Proposal payload must be a JSON object")

    # 1. Exact schema validation (Section 16)
    schema_ver = data.get("schema_version")
    if schema_ver != "hermes.astra-next-action.v1":
        raise AstraProposalInvalidError(
            f"Invalid proposal schema_version '{schema_ver}'. Required: hermes.astra-next-action.v1"
        )

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

    # 2. Check identity
    if data["run_id"] != expected_run_id:
        raise AstraProposalIdentityMismatchError(
            f"Proposal run_id '{data['run_id']}' != expected '{expected_run_id}'"
        )
    if data["task_id"] != expected_task_id:
        raise AstraProposalIdentityMismatchError(
            f"Proposal task_id '{data['task_id']}' != expected '{expected_task_id}'"
        )

    # 3. Check action type
    raw_action = data["action_type"]
    valid_actions = {a.value for a in NextActionType}
    if raw_action not in valid_actions:
        raise AstraProposalActionUnsupportedError(
            f"Unsupported proposal action_type '{raw_action}'. Allowed: {sorted(valid_actions)}"
        )

    # 4. EffectClass Enum Validation (Section 17)
    raw_effect = data["expected_effect_class"]
    try:
        EffectClass(raw_effect)
    except (ValueError, KeyError) as e:
        raise AstraProposalInvalidError(
            f"Invalid expected_effect_class '{raw_effect}' does not parse as EffectClass"
        ) from e

    # 5. StopBoundary Enum Validation (Section 18)
    raw_stop = data["expected_stop_boundary"]
    try:
        StopBoundary(raw_stop)
    except (ValueError, KeyError) as e:
        raise AstraProposalInvalidError(
            f"Invalid expected_stop_boundary '{raw_stop}' does not parse as StopBoundary"
        ) from e

    action_enum = NextActionType(raw_action)

    return AstraNextActionProposal(
        schema_version="hermes.astra-next-action.v1",
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
    """Subprocess-backed Astra Proposal Provider with bounded execution and secret-safe failure receipts."""

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

        from pathlib import Path
        exe_raw = str(self.cmd[0]) if self.cmd else "unknown"
        self.provider_executable = Path(exe_raw).name if exe_raw != "unknown" else "unknown"
        self.command_digest = compute_command_digest(self.cmd)
        self.argument_count = len(self.cmd)

    def _create_receipt(
        self,
        run_id: str,
        task_id: str,
        proposal_id: str,
        action_type: str,
        exit_code: int,
        stdout_digest: str,
        stderr_digest: str,
        duration_ms: int,
        created_at_utc: str,
    ) -> AstraProviderReceipt:
        receipt_data = {
            "schema_version": "hermes.astra-provider-receipt.v1",
            "run_id": run_id,
            "task_id": task_id,
            "proposal_id": proposal_id,
            "action_type": action_type,
            "provider_executable": self.provider_executable,
            "command_digest": self.command_digest,
            "argument_count": self.argument_count,
            "exit_code": exit_code,
            "stdout_digest": stdout_digest,
            "stderr_digest": stderr_digest,
            "duration_ms": duration_ms,
            "created_at_utc": created_at_utc,
        }
        receipt_id = compute_astra_receipt_digest(receipt_data)
        receipt = AstraProviderReceipt(
            schema_version="hermes.astra-provider-receipt.v1",
            receipt_id=receipt_id,
            run_id=run_id,
            task_id=task_id,
            proposal_id=proposal_id,
            action_type=action_type,
            provider_executable=self.provider_executable,
            command_digest=self.command_digest,
            argument_count=self.argument_count,
            exit_code=exit_code,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            duration_ms=duration_ms,
            created_at_utc=created_at_utc,
        )
        self.latest_receipt = receipt
        return receipt

    async def request_proposal(
        self,
        run_id: str,
        state: SupervisorState,
        intent: TaskIntent | None = None,
        budget: BudgetConfig | None = None,
        stats: Mapping[str, int] | None = None,
        work_profile: WorkProfile | None = None,
        effective_policy: EffectivePolicyReport | None = None,
        verified_result: VerifiedResult | None = None,
        context_pack_digest: str | None = None,
        real_mode: bool = False,
        **kwargs: Any,
    ) -> AstraNextActionProposal:
        req_data = build_canonical_astra_request(
            run_id=run_id,
            state=state,
            intent=intent,
            budget=budget,
            stats=stats,
            work_profile=work_profile,
            effective_policy=effective_policy,
            verified_result=verified_result,
            context_pack_digest=context_pack_digest,
            real_mode=real_mode,
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
            duration_ms = int((time.monotonic() - start_time) * 1000)
            self._create_receipt(
                run_id=run_id,
                task_id=state.current_task_id,
                proposal_id="",
                action_type="",
                exit_code=-1,
                stdout_digest="",
                stderr_digest="",
                duration_ms=duration_ms,
                created_at_utc=created_at_utc,
            )
            raise AstraProviderUnavailableError(
                f"ASTRA_PROVIDER_UNAVAILABLE exit_code=-1 provider_executable={self.provider_executable}",
                code="ASTRA_PROVIDER_UNAVAILABLE",
            ) from e

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=req_bytes),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    pass

            self._create_receipt(
                run_id=run_id,
                task_id=state.current_task_id,
                proposal_id="",
                action_type="",
                exit_code=-1,
                stdout_digest="",
                stderr_digest="",
                duration_ms=duration_ms,
                created_at_utc=created_at_utc,
            )
            raise AstraProviderTimeoutError(
                f"ASTRA_PROVIDER_TIMEOUT exit_code=-1 provider_executable={self.provider_executable}",
                code="ASTRA_PROVIDER_TIMEOUT",
            ) from e

        duration_ms = int((time.monotonic() - start_time) * 1000)
        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stdout_digest = hashlib.sha256(stdout_bytes).hexdigest()
        stderr_digest = hashlib.sha256(stderr_bytes).hexdigest()

        if proc.returncode != 0:
            self._create_receipt(
                run_id=run_id,
                task_id=state.current_task_id,
                proposal_id="",
                action_type="",
                exit_code=proc.returncode or -1,
                stdout_digest=stdout_digest,
                stderr_digest=stderr_digest,
                duration_ms=duration_ms,
                created_at_utc=created_at_utc,
            )
            raise AstraProviderUnavailableError(
                f"ASTRA_PROVIDER_UNAVAILABLE exit_code={proc.returncode} provider_executable={self.provider_executable}",
                code="ASTRA_PROVIDER_UNAVAILABLE",
            )

        try:
            parsed_json = json.loads(stdout_str)
        except json.JSONDecodeError as e:
            self._create_receipt(
                run_id=run_id,
                task_id=state.current_task_id,
                proposal_id="",
                action_type="",
                exit_code=proc.returncode or 0,
                stdout_digest=stdout_digest,
                stderr_digest=stderr_digest,
                duration_ms=duration_ms,
                created_at_utc=created_at_utc,
            )
            raise AstraProviderInvalidJsonError(
                f"ASTRA_PROVIDER_INVALID_JSON exit_code={proc.returncode} provider_executable={self.provider_executable}",
                code="ASTRA_PROVIDER_INVALID_JSON",
            ) from e

        try:
            proposal = parse_and_validate_astra_proposal(
                parsed_json,
                expected_run_id=run_id,
                expected_task_id=state.current_task_id,
            )
        except AstraProviderError as e:
            self._create_receipt(
                run_id=run_id,
                task_id=state.current_task_id,
                proposal_id=str(parsed_json.get("proposal_id", "")),
                action_type=str(parsed_json.get("action_type", "")),
                exit_code=0,
                stdout_digest=stdout_digest,
                stderr_digest=stderr_digest,
                duration_ms=duration_ms,
                created_at_utc=created_at_utc,
            )
            raise

        self._create_receipt(
            run_id=run_id,
            task_id=state.current_task_id,
            proposal_id=proposal.proposal_id,
            action_type=proposal.action_type.value,
            exit_code=0,
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            duration_ms=duration_ms,
            created_at_utc=created_at_utc,
        )

        return proposal
