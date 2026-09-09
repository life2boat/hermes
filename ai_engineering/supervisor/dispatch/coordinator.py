"""DispatchCoordinator - coordinates autonomous worker dispatch, CI monitoring,
Astra fix/retry decisions, and source reconciliation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ai_engineering.contracts import EffectClass, Status, StopBoundary
from ai_engineering.effective_policy import EffectivePolicyReport
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.supervisor.decision import (
    ASTRA_DECISION_SCHEMA_VERSION,
    AstraDecision,
    DecisionReceipt,
    SupervisorAction,
    _compute_decision_id,
    decision_content_digest,
)
from ai_engineering.supervisor.dispatch.contracts import (
    DISPATCH_ENVELOPE_SCHEMA_VERSION,
    SOURCE_TRANSITION_RECEIPT_SCHEMA_VERSION,
    DispatchEnvelope,
    DispatchReceipt,
    DispatchStatus,
    SourceTransitionReceipt,
    compute_deterministic_digest,
    compute_dispatch_id,
)
from ai_engineering.supervisor.dispatch.github import CIState, GitHubProvider
from ai_engineering.supervisor.dispatch.worker import WorkerDispatcher
from ai_engineering.supervisor.events import (
    SupervisorEvent,
    SupervisorEventType,
    create_event,
)
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.policy.contracts import (
    AutonomyLevel,
    ExecutionTarget,
    PolicyVerdict,
    WorkProfile,
)
from ai_engineering.supervisor.state import (
    SupervisorError,
    SupervisorPhase,
    SupervisorState,
    _fail,
)
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.validator import (
    VerifiedResult,
    canonical_serialize_verified_result,
    validate_normalized_evidence,
)
from ai_engineering.supervisor.worker_result import (
    WorkerResultBundle,
    deserialize_worker_result,
)
from ai_engineering.task_intent import TaskIntent, deserialize_intent, intent_digest


class DispatchCoordinator:
    """Coordinates autonomous worker dispatch, result collection, CI polling,
    Astra fix/retry decisions, and source reconciliation."""

    def __init__(
        self,
        loop: SupervisorLoop,
        store: FileSupervisorStateStore,
        run_id: str,
        worker_dispatcher: WorkerDispatcher,
        github_provider: GitHubProvider,
        work_profile: WorkProfile | None = None,
        effective_policy: EffectivePolicyReport | None = None,
        evidence_root: Path | str | None = None,
        auto_merge: bool = True,
    ) -> None:
        self.loop = loop
        self.store = store
        self.run_id = run_id
        self.worker_dispatcher = worker_dispatcher
        self.github_provider = github_provider
        self.work_profile = work_profile or getattr(loop, "_profiles", {}).get(run_id)
        self.effective_policy = effective_policy or getattr(loop, "_effective_policies", {}).get(run_id)
        self.evidence_root = Path(evidence_root) if evidence_root else None
        self.auto_merge = auto_merge

        self._staged_worker_results: dict[str, Any] = {}
        self._late_results: list[dict[str, Any]] = []
        self.latest_source_transition_receipt: SourceTransitionReceipt | None = None

        # Load persisted receipt if present
        self._load_persisted_transition_receipt()

    # ── Persistence Helpers ────────────────────────────────────────────────────

    def _intent_file(self, task_id: str) -> Path:
        run_dir = self.store._run_dir(self.run_id)
        return run_dir / f"intent_{task_id}.json"

    def _receipt_file(self) -> Path:
        run_dir = self.store._run_dir(self.run_id)
        return run_dir / "source_transition_receipt.json"

    def _save_intent(self, intent: TaskIntent) -> None:
        self.loop._intents[intent.task_id] = intent
        try:
            target = self._intent_file(intent.task_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            from ai_engineering.task_intent import canonical_serialize_intent
            target.write_text(canonical_serialize_intent(intent), encoding="utf-8")
        except Exception:
            pass

    def _load_intent(self, task_id: str) -> TaskIntent | None:
        if task_id in self.loop._intents:
            return self.loop._intents[task_id]
        if self.run_id in self.loop._intents:
            return self.loop._intents[self.run_id]
        target = self._intent_file(task_id)
        if target.exists():
            try:
                intent = deserialize_intent(target.read_text(encoding="utf-8"))
                self.loop._intents[task_id] = intent
                return intent
            except Exception:
                pass
        return None

    def _save_transition_receipt(self, receipt: SourceTransitionReceipt) -> None:
        self.latest_source_transition_receipt = receipt
        try:
            target = self._receipt_file()
            target.parent.mkdir(parents=True, exist_ok=True)
            d = {
                "schema_version": receipt.schema_version,
                "receipt_id": receipt.receipt_id,
                "repository": receipt.repository,
                "canonical_remote": receipt.canonical_remote,
                "previous_canonical_main_sha": receipt.previous_canonical_main_sha,
                "pr_number": receipt.pr_number,
                "pr_head_sha": receipt.pr_head_sha,
                "merge_method": receipt.merge_method,
                "merge_commit_sha": receipt.merge_commit_sha,
                "new_canonical_main_sha": receipt.new_canonical_main_sha,
                "reconciliation_evidence": receipt.reconciliation_evidence,
                "created_at_utc": receipt.created_at_utc,
            }
            target.write_text(json.dumps(d, sort_keys=True, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_persisted_transition_receipt(self) -> None:
        target = self._receipt_file()
        if target.exists():
            try:
                d = json.loads(target.read_text(encoding="utf-8"))
                self.latest_source_transition_receipt = SourceTransitionReceipt(
                    schema_version=d["schema_version"],
                    receipt_id=d["receipt_id"],
                    repository=d["repository"],
                    canonical_remote=d["canonical_remote"],
                    previous_canonical_main_sha=d["previous_canonical_main_sha"],
                    pr_number=d.get("pr_number"),
                    pr_head_sha=d.get("pr_head_sha"),
                    merge_method=d.get("merge_method"),
                    merge_commit_sha=d.get("merge_commit_sha"),
                    new_canonical_main_sha=d["new_canonical_main_sha"],
                    reconciliation_evidence=d["reconciliation_evidence"],
                    created_at_utc=d["created_at_utc"],
                )
            except Exception:
                pass

    # ── State Access ───────────────────────────────────────────────────────────

    def get_state(self) -> SupervisorState:
        return self.store.load_state(self.run_id)

    # ── External Interaction Methods ───────────────────────────────────────────

    def stage_worker_result(self, dispatch_id: str, result: Any) -> None:
        """Stage a result for a specific dispatch ID."""
        state = self.get_state()
        is_late, reason = self._is_late_result(result, state)
        if is_late:
            self._late_results.append({
                "rejected_at_utc": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "raw_result": str(result),
            })
            return
        self._staged_worker_results[dispatch_id] = result

    def submit_worker_result(self, result: Any) -> None:
        """Submit a result for the currently active dispatch."""
        state = self.get_state()
        is_late, reason = self._is_late_result(result, state)
        if is_late:
            self._late_results.append({
                "rejected_at_utc": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "raw_result": str(result),
            })
            return

        if state.active_dispatch_id:
            self._staged_worker_results[state.active_dispatch_id] = result
        else:
            dispatch_id = compute_dispatch_id(
                self.run_id,
                state.current_task_id,
                state.current_attempt_id,
                state.current_intent_digest,
            )
            self._staged_worker_results[dispatch_id] = result

    def bind_pr(self, pr_number: int, pr_head_sha: str) -> SupervisorState:
        """Bind a GitHub PR to the active run state."""
        state = self.get_state()
        events = self.store.load_events(self.run_id)
        now_utc = datetime.now(timezone.utc).isoformat()

        event = create_event(
            run_id=self.run_id,
            sequence=len(events) + 1,
            previous_event_digest=events[-1].event_digest if events else None,
            event_type=SupervisorEventType.PR_BOUND,
            state_revision=state.state_revision + 1,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            payload={"pr_number": pr_number, "pr_head_sha": pr_head_sha},
            created_at_utc=now_utc,
            decision_id=f"pr-{pr_number}",
            verified_result_id=pr_head_sha,
        )
        self.store.save_event(event)

        # Update seed state so that replay retains PR details
        seed = self.store.load_seed_state(self.run_id)
        updated_seed = replace(seed, pr_number=pr_number, pr_head_sha=pr_head_sha)
        self.store.save_seed_state(updated_seed)

        return self.get_state()

    # ── State Machine Step ─────────────────────────────────────────────────────

    def step(self) -> SupervisorState:
        """Execute one step of the supervisor state machine."""
        state = self.get_state()

        if state.phase in (SupervisorPhase.DONE, SupervisorPhase.FAILED, SupervisorPhase.CANCELLED):
            return state

        if state.phase == SupervisorPhase.BLOCKED:
            return state

        if state.phase == SupervisorPhase.RUNNING:
            return self._handle_running(state)

        if state.phase == SupervisorPhase.VERIFYING:
            return self._handle_verifying(state)

        if state.phase == SupervisorPhase.DECIDING:
            return self._handle_deciding(state)

        if state.phase == SupervisorPhase.READY_FOR_NEXT_TASK:
            return self._handle_ready_for_next_task(state)

        return state

    def run_until_quiescent(self, max_steps: int = 50) -> SupervisorState:
        """Step repeatedly until a terminal phase is reached or waiting on external inputs."""
        for _ in range(max_steps):
            prev_rev = self.get_state().state_revision
            state = self.step()
            if state.phase in (
                SupervisorPhase.DONE,
                SupervisorPhase.FAILED,
                SupervisorPhase.CANCELLED,
                SupervisorPhase.BLOCKED,
            ):
                return state
            if state.state_revision == prev_rev:
                # No progress made this step (waiting on worker or CI)
                break
        return self.get_state()

    # ── Phase Handlers ─────────────────────────────────────────────────────────

    def _handle_running(self, state: SupervisorState) -> SupervisorState:
        """Handle state in RUNNING phase: dispatch if needed, or monitor worker result."""
        expected_dispatch_id = compute_dispatch_id(
            self.run_id,
            state.current_task_id,
            state.current_attempt_id,
            state.current_intent_digest,
        )

        # 1. Dispatch envelope if not already active
        if state.active_dispatch_id != expected_dispatch_id:
            return self._dispatch_current_task(state, expected_dispatch_id)

        # 2. If active dispatch exists, monitor worker for result
        return self._monitor_worker_result(state, expected_dispatch_id)

    def _dispatch_current_task(self, state: SupervisorState, dispatch_id: str) -> SupervisorState:
        now_utc = datetime.now(timezone.utc).isoformat()
        intent = self._load_intent(state.current_task_id)

        envelope = DispatchEnvelope(
            schema_version=DISPATCH_ENVELOPE_SCHEMA_VERSION,
            dispatch_id=dispatch_id,
            run_id=self.run_id,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            intent_revision=state.current_intent_revision,
            source_repository=state.repository,
            source_main_ref=state.canonical_main_ref,
            source_base_sha=state.current_base_sha,
            task_intent_digest=state.current_intent_digest,
            policy_receipt_id=f"policy-{state.current_task_id}",
            policy_receipt_digest=hashlib.sha256(f"policy-{state.current_task_id}".encode()).hexdigest(),
            worker_kind=self.work_profile.profile_id if self.work_profile else "standard-worker",
            worker_profile=self.work_profile.profile_id if self.work_profile else "default",
            worker_model=(self.work_profile.preferred_worker_model if self.work_profile else None) or "gemini-pro",
            required_result_schema="hermes.verified-result.v1",
            required_gates=tuple(intent.required_gates) if intent else ("tests", "lint"),
            created_at_utc=now_utc,
        )

        receipt = self.worker_dispatcher.dispatch(envelope)
        events = self.store.load_events(self.run_id)

        if receipt.status == DispatchStatus.DISPATCHED:
            event = create_event(
                run_id=self.run_id,
                sequence=len(events) + 1,
                previous_event_digest=events[-1].event_digest if events else None,
                event_type=SupervisorEventType.DISPATCH_SENT,
                state_revision=state.state_revision + 1,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={
                    "dispatch_id": envelope.dispatch_id,
                    "worker_identity": receipt.worker_identity,
                    "receipt_id": receipt.receipt_id,
                },
                created_at_utc=now_utc,
                decision_id=envelope.dispatch_id,
            )
            self.store.save_event(event)

            # Update seed state for replay fidelity
            seed = self.store.load_seed_state(self.run_id)
            updated_seed = replace(
                seed,
                active_dispatch_id=envelope.dispatch_id,
                active_dispatch_digest=event.payload_digest,
            )
            self.store.save_seed_state(updated_seed)
            return self.get_state()

        else:
            # Dispatch failed
            blocker_msg = f"DISPATCH_FAILED: {receipt.reason_code or 'UNKNOWN'}"
            event = create_event(
                run_id=self.run_id,
                sequence=len(events) + 1,
                previous_event_digest=events[-1].event_digest if events else None,
                event_type=SupervisorEventType.BLOCKER_RECORDED,
                state_revision=state.state_revision + 1,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={"blocker": blocker_msg, "reason_code": receipt.reason_code},
                created_at_utc=now_utc,
            )
            self.store.save_event(event)
            return self.get_state()

    def _monitor_worker_result(self, state: SupervisorState, dispatch_id: str) -> SupervisorState:
        # Check staged or polled results
        raw_result = None
        if hasattr(self.worker_dispatcher, "poll_result"):
            raw_result = self.worker_dispatcher.poll_result(dispatch_id)
        if raw_result is None:
            raw_result = self._staged_worker_results.get(dispatch_id)

        if raw_result is None:
            # Worker still working
            return state

        # Check for LATE RESULT guard
        is_late, reason = self._is_late_result(raw_result, state)
        if is_late:
            self._late_results.append({
                "rejected_at_utc": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "raw_result": str(raw_result),
            })
            # Remove from staged results so we don't process again
            self._staged_worker_results.pop(dispatch_id, None)
            return state

        # Normalize and ingest result
        vr = self._coerce_to_verified_result(raw_result, state)
        vr_digest = hashlib.sha256(canonical_serialize_verified_result(vr).encode("utf-8")).hexdigest()

        # Ingest into supervisor loop
        new_state = self.loop.ingest_verified_result(self.run_id, vr, vr_digest)

        # Update seed so that latest_verified_result_digest and id persist across store.load_state
        seed = self.store.load_seed_state(self.run_id)
        updated_seed = replace(
            seed,
            latest_verified_result_id=vr.result_id,
            latest_verified_result_digest=vr_digest,
        )
        self.store.save_seed_state(updated_seed)

        # Emit WORKER_RESULT_RECEIVED event
        events = self.store.load_events(self.run_id)
        now_utc = datetime.now(timezone.utc).isoformat()
        ev = create_event(
            run_id=self.run_id,
            sequence=len(events) + 1,
            previous_event_digest=events[-1].event_digest if events else None,
            event_type=SupervisorEventType.WORKER_RESULT_RECEIVED,
            state_revision=new_state.state_revision + 1,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            payload={
                "result_id": vr.result_id,
                "result_digest": vr_digest,
                "status": vr.status.value,
            },
            created_at_utc=now_utc,
            verified_result_id=vr.result_id,
        )
        self.store.save_event(ev)

        # Clean staged result
        self._staged_worker_results.pop(dispatch_id, None)

        # Automatic PR binding if available on result or provider
        self._auto_bind_pr_if_available(raw_result, vr)

        return self.get_state()

    def _is_late_result(self, raw_result: Any, state: SupervisorState) -> tuple[bool, str]:
        """Verify whether the result matches current task, attempt, and intent."""
        if state.phase in (SupervisorPhase.DONE, SupervisorPhase.FAILED, SupervisorPhase.CANCELLED):
            return True, "TERMINAL_STATE"

        task_id = getattr(raw_result, "task_id", None)
        attempt_id = getattr(raw_result, "attempt_id", None)
        idg = getattr(raw_result, "intent_digest", None)

        if isinstance(raw_result, dict):
            task_id = raw_result.get("task_id", task_id)
            attempt_id = raw_result.get("attempt_id", attempt_id)
            idg = raw_result.get("intent_digest", idg)

        if task_id and task_id != state.current_task_id:
            return True, f"TASK_MISMATCH: expected {state.current_task_id}, got {task_id}"
        if attempt_id and attempt_id != state.current_attempt_id:
            return True, f"ATTEMPT_MISMATCH: expected {state.current_attempt_id}, got {attempt_id}"
        if idg and idg != state.current_intent_digest:
            return True, f"INTENT_MISMATCH: expected {state.current_intent_digest}, got {idg}"

        return False, ""

    def _coerce_to_verified_result(self, raw: Any, state: SupervisorState) -> VerifiedResult:
        """Convert raw result or worker bundle into VerifiedResult."""
        if isinstance(raw, VerifiedResult):
            return raw

        if isinstance(raw, (dict, str)):
            d = raw if isinstance(raw, dict) else json.loads(raw)
            sv = d.get("schema_version") if isinstance(d, dict) else None
            if sv == "hermes.verified-result.v1":
                from ai_engineering.supervisor.validator import deserialize_verified_result
                return deserialize_verified_result(json.dumps(d))

            if isinstance(raw, WorkerResultBundle):
                bundle = raw
            else:
                bundle = deserialize_worker_result(json.dumps(d))

            intent = self._load_intent(state.current_task_id)
            if intent is None:
                _fail("INTENT_NOT_FOUND")

            collector = ResultCollector(evidence_root=self.evidence_root or Path("/tmp"))
            normalized = collector.collect(bundle, intent)
            return validate_normalized_evidence(normalized, intent)

        _fail(f"UNSUPPORTED_RESULT_TYPE:{type(raw)}")

    def _auto_bind_pr_if_available(self, raw_result: Any, vr: VerifiedResult) -> None:
        """Attempt to bind a PR if metadata is found on the raw result or provider."""
        state = self.get_state()
        if state.pr_number is not None:
            return

        pr_num = getattr(raw_result, "pr_number", None)
        pr_sha = getattr(raw_result, "pr_head_sha", None) or getattr(vr, "head_sha", None)

        if isinstance(raw_result, dict):
            pr_num = raw_result.get("pr_number", pr_num)
            pr_sha = raw_result.get("pr_head_sha", pr_sha)

        if pr_num is None and hasattr(self.github_provider, "prs"):
            prs = getattr(self.github_provider, "prs", {})
            if prs:
                first_num = next(iter(prs.keys()))
                pr = prs[first_num]
                pr_num = pr.pr_number
                pr_sha = pr.head_sha

        if pr_num is not None:
            head_sha = pr_sha or state.current_base_sha
            self.bind_pr(pr_num, head_sha)

    def _handle_verifying(self, state: SupervisorState) -> SupervisorState:
        """Handle state in VERIFYING phase: check CI exact-head state, handle PASS/FAIL/BLOCKED."""
        # 1. If PR is bound, poll GitHub CI
        if state.pr_number is not None:
            head_sha = state.pr_head_sha or state.current_base_sha
            ci = self.github_provider.get_ci_state(state.repository, state.pr_number, head_sha)

            if ci is None or ci.overall_state == CIState.PENDING:
                # CI still in progress
                return state

            events = self.store.load_events(self.run_id)
            now_utc = datetime.now(timezone.utc).isoformat()

            if ci.overall_state == CIState.PASS:
                # Emit CI_PASSED
                event = create_event(
                    run_id=self.run_id,
                    sequence=len(events) + 1,
                    previous_event_digest=events[-1].event_digest if events else None,
                    event_type=SupervisorEventType.CI_PASSED,
                    state_revision=state.state_revision + 1,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    payload={"ci_state": "PASS", "head_sha": head_sha},
                    created_at_utc=now_utc,
                )
                self.store.save_event(event)

                # Reconcile merge if authorized
                return self._reconcile_pass(self.get_state())

            elif ci.overall_state == CIState.FAIL:
                # Emit CI_FAILED
                event = create_event(
                    run_id=self.run_id,
                    sequence=len(events) + 1,
                    previous_event_digest=events[-1].event_digest if events else None,
                    event_type=SupervisorEventType.CI_FAILED,
                    state_revision=state.state_revision + 1,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    payload={"ci_state": "FAIL", "head_sha": head_sha},
                    created_at_utc=now_utc,
                )
                self.store.save_event(event)

                # Automatic FIX decision via Astra
                return self._trigger_automatic_decision(self.get_state(), SupervisorAction.FIX, "CI_FAIL")

            elif ci.overall_state == CIState.BLOCKED:
                # Emit CI_FAILED
                event = create_event(
                    run_id=self.run_id,
                    sequence=len(events) + 1,
                    previous_event_digest=events[-1].event_digest if events else None,
                    event_type=SupervisorEventType.CI_FAILED,
                    state_revision=state.state_revision + 1,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    payload={"ci_state": "BLOCKED", "head_sha": head_sha},
                    created_at_utc=now_utc,
                )
                self.store.save_event(event)

                # Automatic RETRY decision via Astra
                return self._trigger_automatic_decision(self.get_state(), SupervisorAction.RETRY, "CI_BLOCKED")

        # 2. No PR bound: check VerifiedResult directly
        vr = self.loop._vr_cache.get(state.latest_verified_result_id or "")
        if vr is None:
            return state

        if vr.status == Status.PASS:
            return self._reconcile_pass(state)
        elif vr.status == Status.FAIL:
            return self._trigger_automatic_decision(state, SupervisorAction.FIX, "VERIFIED_RESULT_FAIL")
        elif vr.status == Status.BLOCKED:
            return self._trigger_automatic_decision(state, SupervisorAction.RETRY, "VERIFIED_RESULT_BLOCKED")

        return state

    # ── Reconciliation & Decision Generation ───────────────────────────────────

    def _is_merge_authorized(self, state: SupervisorState) -> bool:
        """Check whether merging is authorized by task intent, stop boundary, and autonomy."""
        task_intent = self._load_intent(state.current_task_id)
        if task_intent:
            if EffectClass.PR_MERGE in task_intent.forbidden_mutations:
                return False
            if task_intent.stop_boundary in (
                StopBoundary.READ_ONLY,
                StopBoundary.LOCAL_DIFF,
                StopBoundary.COMMIT,
                StopBoundary.DRAFT_PR,
            ):
                return False

        if self.work_profile:
            if EffectClass.PR_MERGE in self.work_profile.forbidden_effect_classes:
                return False
            if self.work_profile.maximum_autonomy_level in (
                AutonomyLevel.LEVEL_0_OBSERVE,
                AutonomyLevel.LEVEL_1_LOCAL_WRITE,
                AutonomyLevel.LEVEL_2_DEV_AUTONOMY,
            ):
                return False

        return True

    def _reconcile_pass(self, state: SupervisorState) -> SupervisorState:
        """Reconcile merge, update base_sha, and complete run when CI passes and merge is authorized."""
        now_utc = datetime.now(timezone.utc).isoformat()
        events = self.store.load_events(self.run_id)

        if self.auto_merge and self._is_merge_authorized(state):
            # 1. Execute squash merge (simulated via provider or sha calculation)
            merge_commit_sha = None
            if hasattr(self.github_provider, "merge_pr") and state.pr_number is not None:
                merge_commit_sha = self.github_provider.merge_pr(state.repository, state.pr_number, "squash")
            if not merge_commit_sha:
                pr_str = str(state.pr_number or "direct")
                head_str = state.pr_head_sha or state.current_base_sha
                merge_commit_sha = hashlib.sha256(
                    f"merge-{state.repository}-{pr_str}-{head_str}-squash".encode("utf-8")
                ).hexdigest()[:40]

            # 2. Create SourceTransitionReceipt
            receipt_id = f"st-receipt-{state.run_id}-{state.state_revision + 1}"
            st_receipt = SourceTransitionReceipt(
                schema_version=SOURCE_TRANSITION_RECEIPT_SCHEMA_VERSION,
                receipt_id=receipt_id,
                repository=state.repository,
                canonical_remote=state.canonical_remote,
                previous_canonical_main_sha=state.current_base_sha,
                pr_number=state.pr_number,
                pr_head_sha=state.pr_head_sha,
                merge_method="squash",
                merge_commit_sha=merge_commit_sha,
                new_canonical_main_sha=merge_commit_sha,
                reconciliation_evidence=f"PR #{state.pr_number} squash merged at {merge_commit_sha}",
                created_at_utc=now_utc,
            )
            self._save_transition_receipt(st_receipt)

            # 3. Emit SOURCE_RECONCILED event
            rec_event = create_event(
                run_id=self.run_id,
                sequence=len(events) + 1,
                previous_event_digest=events[-1].event_digest if events else None,
                event_type=SupervisorEventType.SOURCE_RECONCILED,
                state_revision=state.state_revision + 1,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={
                    "receipt_id": st_receipt.receipt_id,
                    "merge_method": "squash",
                    "merge_commit_sha": merge_commit_sha,
                    "new_canonical_main_sha": merge_commit_sha,
                    "previous_canonical_main_sha": state.current_base_sha,
                },
                created_at_utc=now_utc,
                verified_result_id=merge_commit_sha,
            )
            self.store.save_event(rec_event)

            # 4. Emit RUN_COMPLETED event
            events = self.store.load_events(self.run_id)
            comp_event = create_event(
                run_id=self.run_id,
                sequence=len(events) + 1,
                previous_event_digest=events[-1].event_digest if events else None,
                event_type=SupervisorEventType.RUN_COMPLETED,
                state_revision=state.state_revision + 2,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={"new_base_sha": merge_commit_sha},
                created_at_utc=now_utc,
                verified_result_id=merge_commit_sha,
            )
            self.store.save_event(comp_event)

            # Update seed state so that replay returns DONE and retains PR/CI state
            seed = self.store.load_seed_state(self.run_id)
            updated_seed = replace(
                seed,
                current_base_sha=merge_commit_sha,
                active_dispatch_id=None,
                active_dispatch_digest=None,
                pr_number=state.pr_number,
                pr_head_sha=state.pr_head_sha,
                ci_state="PASS",
            )
            self.store.save_seed_state(updated_seed)

            return self.get_state()

        else:
            # Stop boundary reached or merge not authorized
            comp_event = create_event(
                run_id=self.run_id,
                sequence=len(events) + 1,
                previous_event_digest=events[-1].event_digest if events else None,
                event_type=SupervisorEventType.RUN_COMPLETED,
                state_revision=state.state_revision + 1,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={"stop_boundary": "MERGE_NOT_AUTHORIZED_OR_AUTO_DISABLED"},
                created_at_utc=now_utc,
            )
            self.store.save_event(comp_event)
            return self.get_state()

    def _trigger_automatic_decision(
        self,
        state: SupervisorState,
        action: SupervisorAction,
        reason: str,
    ) -> SupervisorState:
        """Trigger an offline simulated Astra decision (FIX or RETRY) and process via policy."""
        now_utc = datetime.now(timezone.utc).isoformat()
        task_intent = self._load_intent(state.current_task_id)
        if task_intent is None:
            _fail("INTENT_NOT_FOUND")

        # 1. Build context pack
        pack, state = self.loop.build_context_pack(self.run_id, task_intent)
        from ai_engineering.supervisor.context_pack import context_pack_digest
        pack_dg = context_pack_digest(pack)

        vr_id = state.latest_verified_result_id or "vr-placeholder"
        vr_digest = (
            self.loop._vr_cache.get(f"digest:{vr_id}")
            or state.latest_verified_result_digest
            or ""
        )
        if not vr_digest and vr_id in self.loop._vr_cache:
            vr = self.loop._vr_cache[vr_id]
            vr_digest = hashlib.sha256(canonical_serialize_verified_result(vr).encode("utf-8")).hexdigest()
        if not vr_digest:
            vr_digest = "v" * 64

        # 2. Form simulated Astra decision
        decision = self._create_simulated_astra_decision(
            state=state,
            action=action,
            pack_digest=pack_dg,
            vr_id=vr_id,
            vr_digest=vr_digest,
            rationale=f"Simulated Astra auto-{action.value}: {reason}",
            next_objective=f"Resolve {reason}",
        )

        status_str = "FAIL" if action == SupervisorAction.FIX else "BLOCKED"
        new_task_id = f"{state.current_task_id}-fix-{state.attempt_number}" if action == SupervisorAction.FIX else state.current_task_id
        new_attempt_id = f"{new_task_id}-attempt-{state.attempt_number + 1}"

        # 3. Accept decision in loop (evaluates policy & budget)
        receipt, updated_state = self.loop.accept_decision(
            run_id=self.run_id,
            decision=decision,
            context_pack_digest=pack_dg,
            verified_result_status=status_str,
            validated_at_utc=now_utc,
            parent_intent=task_intent,
            effective_policy=self.effective_policy,
            new_task_id=new_task_id,
            new_base_sha=state.current_base_sha,
            new_attempt_id=new_attempt_id,
        )

        # Save any new task intent
        if updated_state.current_task_id in self.loop._intents:
            self._save_intent(self.loop._intents[updated_state.current_task_id])

        # If policy allowed, reset active_dispatch_id on seed so fresh dispatch is triggered
        if updated_state.phase == SupervisorPhase.RUNNING:
            seed = self.store.load_seed_state(self.run_id)
            updated_seed = replace(
                seed,
                active_dispatch_id=None,
                active_dispatch_digest=None,
            )
            self.store.save_seed_state(updated_seed)

        if updated_state.phase == SupervisorPhase.BLOCKED:
            return updated_state

        return self.get_state()

    def _create_simulated_astra_decision(
        self,
        state: SupervisorState,
        action: SupervisorAction,
        pack_digest: str,
        vr_id: str,
        vr_digest: str,
        rationale: str | None = None,
        next_objective: str | None = None,
    ) -> AstraDecision:
        now_utc = datetime.now(timezone.utc).isoformat()
        rat = rationale or f"Offline simulated Astra decision: {action.value}"
        d = {
            "acceptance_delta": None,
            "action": action.value,
            "attempt_id": state.current_attempt_id,
            "context_pack_digest": pack_digest,
            "created_at_utc": now_utc,
            "decision_id": "",
            "intent_digest": state.current_intent_digest,
            "next_objective": next_objective,
            "rationale_summary": rat,
            "requested_required_gates": ["tests", "lint"],
            "run_id": state.run_id,
            "schema_version": ASTRA_DECISION_SCHEMA_VERSION,
            "task_id": state.current_task_id,
            "verified_result_digest": vr_digest,
            "verified_result_id": vr_id,
        }
        dec_id = _compute_decision_id(d)
        return AstraDecision(
            schema_version=ASTRA_DECISION_SCHEMA_VERSION,
            decision_id=dec_id,
            run_id=state.run_id,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            verified_result_id=vr_id,
            verified_result_digest=vr_digest,
            context_pack_digest=pack_digest,
            action=action,
            rationale_summary=rat,
            next_objective=next_objective,
            acceptance_delta=None,
            requested_required_gates=("tests", "lint"),
            created_at_utc=now_utc,
        )

    def _handle_deciding(self, state: SupervisorState) -> SupervisorState:
        vr = self.loop._vr_cache.get(state.latest_verified_result_id or "")
        action = SupervisorAction.CONTINUE if vr and vr.status == Status.PASS else SupervisorAction.FIX
        return self._trigger_automatic_decision(state, action, "MANUAL_DECIDING")

    def _handle_ready_for_next_task(self, state: SupervisorState) -> SupervisorState:
        return state
