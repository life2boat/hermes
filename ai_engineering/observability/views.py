"""Immutable operator-facing projection views (PR-12).

Every view is a frozen, typed, allowlisted representation of an
authoritative record. Views never expose raw prompts, credentials,
worktree filesystem paths, or unbounded payloads. Views never hold
references to authoritative objects — only scalar projections.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ai_engineering.observability.contracts import BarrierName

REDACTED = "<REDACTED>"


def _tuple_of_str(values: tuple[str, ...] | list[str]) -> list[str]:
    return list(values)


# --------------------------------------------------------------------------
# Cycle / control plane / intent / lineage / authority
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ControlPlaneView:
    """Control-plane orchestration projection (phase + readiness semantics)."""

    phase: str
    terminal: bool
    blocked: bool
    requalification_required: bool
    handoff_ready: bool
    blockers: tuple[str, ...]
    selected_candidate_id: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "terminal": self.terminal,
            "blocked": self.blocked,
            "requalification_required": self.requalification_required,
            "handoff_ready": self.handoff_ready,
            "blockers": _tuple_of_str(self.blockers),
            "selected_candidate_id": self.selected_candidate_id,
        }


@dataclass(frozen=True, slots=True)
class CycleView:
    """Safe cycle identity projection. Raw TaskIntent prompt text is never exposed."""

    cycle_id: str
    task_id: str
    node_id: str
    intent_digest: str
    intent_revision: int
    repository_id: str
    source_base_sha: str
    execution_epoch: int
    selected_strategy: str
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "cycle_id": self.cycle_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "intent_id": self.intent_digest,
            "intent_digest": self.intent_digest,
            "intent_revision": self.intent_revision,
            "repository_id": self.repository_id,
            "source_base_sha": self.source_base_sha,
            "execution_epoch": self.execution_epoch,
            "selected_strategy": self.selected_strategy,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class TaskIntentView:
    """Safe TaskIntent metadata. desired_outcome / constraints text is never exposed."""

    task_id: str
    intent_digest: str
    intent_revision: int
    status: str
    task_class: str
    source_repository: str
    source_main_ref: str
    source_base_sha: str
    stop_boundary: str
    acceptance_criteria_count: int
    unknowns_count: int
    blocking_unknowns_count: int
    applicable_invariants: tuple[str, ...]
    required_gates: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "intent_digest": self.intent_digest,
            "intent_revision": self.intent_revision,
            "status": self.status,
            "task_class": self.task_class,
            "source_repository": self.source_repository,
            "source_main_ref": self.source_main_ref,
            "source_base_sha": self.source_base_sha,
            "stop_boundary": self.stop_boundary,
            "acceptance_criteria_count": self.acceptance_criteria_count,
            "unknowns_count": self.unknowns_count,
            "blocking_unknowns_count": self.blocking_unknowns_count,
            "applicable_invariants": _tuple_of_str(self.applicable_invariants),
            "required_gates": _tuple_of_str(self.required_gates),
        }


@dataclass(frozen=True, slots=True)
class TaskLineageView:
    """Safe TaskLineage binding projection."""

    node_count: int
    edge_count: int
    bound_node_id: str | None
    bound_node_kind: str | None
    bound_node_present: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "bound_node_id": self.bound_node_id,
            "bound_node_kind": self.bound_node_kind,
            "bound_node_present": self.bound_node_present,
        }


@dataclass(frozen=True, slots=True)
class AuthorityView:
    """Descriptive effective-authority projection. Grants/revokes nothing."""

    allowed_effect_classes: tuple[str, ...]
    forbidden_effect_classes: tuple[str, ...]
    stop_boundary: str
    production_authorized: bool
    secret_access_authorized: bool
    data_access_authorized: bool
    authority_restricted: bool
    authority_production_capable: bool
    authority_secret_capable: bool
    authority_datastore_capable: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed_effect_classes": _tuple_of_str(self.allowed_effect_classes),
            "forbidden_effect_classes": _tuple_of_str(self.forbidden_effect_classes),
            "stop_boundary": self.stop_boundary,
            "production_authorized": self.production_authorized,
            "secret_access_authorized": self.secret_access_authorized,
            "data_access_authorized": self.data_access_authorized,
            "authority_restricted": self.authority_restricted,
            "authority_production_capable": self.authority_production_capable,
            "authority_secret_capable": self.authority_secret_capable,
            "authority_datastore_capable": self.authority_datastore_capable,
        }


# --------------------------------------------------------------------------
# Workspaces / leases / runs / hosts / parallelization
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkspaceView:
    """Workspace identity projection. Raw worktree paths are never serialized."""

    workspace_id: str
    task_id: str
    candidate_id: str | None
    repository: str
    base_ref: str
    base_sha: str
    branch: str
    execution_host_id: str
    execution_mode: str
    lease_state: str | None
    owner_run_id: str | None
    created_at: str
    worktree_path_disclosure: str

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "repository": self.repository,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "branch": self.branch,
            "execution_host_id": self.execution_host_id,
            "execution_mode": self.execution_mode,
            "lease_state": self.lease_state,
            "owner_run_id": self.owner_run_id,
            "created_at": self.created_at,
            "worktree_path_disclosure": self.worktree_path_disclosure,
        }


@dataclass(frozen=True, slots=True)
class LeaseView:
    """Worktree lease projection with deterministic classification."""

    workspace_id: str
    owner_run_id: str
    task_id: str
    state: str
    acquired_at: str
    expires_at: str | None
    classification: str
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "workspace_id": self.workspace_id,
            "owner_run_id": self.owner_run_id,
            "task_id": self.task_id,
            "state": self.state,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "classification": self.classification,
            "reason_codes": _tuple_of_str(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class RunView:
    """Agent run projection with operator-safe derived state."""

    run_id: str
    task_id: str
    node_id: str
    workspace_id: str
    candidate_id: str | None
    model: str
    agent_capability: str
    execution_host_id: str
    execution_epoch: int
    state: str
    operator_state: str
    start_time: str
    exit_code: int | None
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "workspace_id": self.workspace_id,
            "candidate_id": self.candidate_id,
            "model": self.model,
            "agent_capability": self.agent_capability,
            "execution_host_id": self.execution_host_id,
            "execution_epoch": self.execution_epoch,
            "state": self.state,
            "operator_state": self.operator_state,
            "start_time": self.start_time,
            "exit_code": self.exit_code,
            "reason_codes": _tuple_of_str(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class ExecutionHostView:
    """Execution host projection. Remote UNVERIFIABLE stays distinct from EXITED."""

    execution_host_id: str
    mode: str
    available: bool
    capabilities: tuple[str, ...]
    remote_reconciliation_required: bool
    remote_state: str | None
    remote_reconciled_terminal: bool
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_host_id": self.execution_host_id,
            "mode": self.mode,
            "available": self.available,
            "capabilities": _tuple_of_str(self.capabilities),
            "remote_reconciliation_required": self.remote_reconciliation_required,
            "remote_state": self.remote_state,
            "remote_reconciled_terminal": self.remote_reconciled_terminal,
            "reason_codes": _tuple_of_str(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class ParallelizationView:
    """Parallelization projection (descriptive; never enforces by mutation)."""

    strategy: str
    budget_max_candidates: int | None
    active_candidates: int
    terminal_candidates: int
    active_mutation_candidates: int
    candidate_slots_used: int
    candidate_slots_remaining: int | None
    concurrency_status: str
    requires_single_mutation_owner: bool | None
    requires_serialization_barrier: bool | None

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "budget_max_candidates": self.budget_max_candidates,
            "active_candidates": self.active_candidates,
            "terminal_candidates": self.terminal_candidates,
            "active_mutation_candidates": self.active_mutation_candidates,
            "candidate_slots_used": self.candidate_slots_used,
            "candidate_slots_remaining": self.candidate_slots_remaining,
            "concurrency_status": self.concurrency_status,
            "requires_single_mutation_owner": self.requires_single_mutation_owner,
            "requires_serialization_barrier": self.requires_serialization_barrier,
        }


# --------------------------------------------------------------------------
# Candidates / judgement / validation / requalification / handoff
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateView:
    """Candidate projection bound by canonical CandidateIdentity semantics."""

    candidate_id: str
    task_id: str
    node_id: str
    workspace_id: str
    run_id: str
    base_sha: str
    completion_state: str
    validation_eligible: bool
    judgement_eligible: bool
    freshness: str | None
    selected: bool
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "base_sha": self.base_sha,
            "completion_state": self.completion_state,
            "validation_eligible": self.validation_eligible,
            "judgement_eligible": self.judgement_eligible,
            "freshness": self.freshness,
            "selected": self.selected,
            "reason_codes": _tuple_of_str(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class CandidateJudgementView:
    """Per-candidate judgement projection. Hard validation is displayed as logically
    stronger than semantic review: a failed hard gate always renders ineligible."""

    candidate_id: str
    hard_gate_passed: bool
    eligible: bool
    semantic_score: float | None
    semantic_review_present: bool
    rank: int | None
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "hard_gate_passed": self.hard_gate_passed,
            "eligible": self.eligible,
            "semantic_score": self.semantic_score,
            "semantic_review_present": self.semantic_review_present,
            "rank": self.rank,
            "blockers": _tuple_of_str(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class JudgementView:
    """Deterministic judgement outcome projection."""

    present: bool
    judge_id: str | None
    task_id: str | None
    node_id: str | None
    base_sha: str | None
    decision_state: str | None
    selected_candidate_id: str | None
    judgements: tuple[CandidateJudgementView, ...]
    completed_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "present": self.present,
            "judge_id": self.judge_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "base_sha": self.base_sha,
            "decision_state": self.decision_state,
            "selected_candidate_id": self.selected_candidate_id,
            "judgements": [j.to_dict() for j in self.judgements],
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class ValidationView:
    """Validation evidence projection (summaries only, never payload contents)."""

    present: bool
    evidence_id: str | None
    candidate_id: str | None
    cycle_binding_ok: bool | None
    task_binding_ok: bool | None
    node_binding_ok: bool | None
    base_sha_binding_ok: bool | None
    execution_epoch_binding_ok: bool | None
    base_sha: str | None
    execution_epoch: int | None
    evidence_refs_count: int
    evidence_refs: tuple[str, ...]
    freshness: str | None
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "present": self.present,
            "evidence_id": self.evidence_id,
            "candidate_id": self.candidate_id,
            "cycle_binding_ok": self.cycle_binding_ok,
            "task_binding_ok": self.task_binding_ok,
            "node_binding_ok": self.node_binding_ok,
            "base_sha_binding_ok": self.base_sha_binding_ok,
            "execution_epoch_binding_ok": self.execution_epoch_binding_ok,
            "base_sha": self.base_sha,
            "execution_epoch": self.execution_epoch,
            "evidence_refs_count": self.evidence_refs_count,
            "evidence_refs": _tuple_of_str(self.evidence_refs),
            "freshness": self.freshness,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class RequalificationView:
    """Requalification / main-drift projection. No fetching, no auto-rebase."""

    requalification_required: bool
    decision_state: str | None
    candidate_base_sha: str | None
    qualified_against_sha: str | None
    current_authoritative_sha: str | None
    relationship: str | None
    validation_rerun_required: bool | None
    judgement_rerun_required: bool | None
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "requalification_required": self.requalification_required,
            "decision_state": self.decision_state,
            "candidate_base_sha": self.candidate_base_sha,
            "qualified_against_sha": self.qualified_against_sha,
            "current_authoritative_sha": self.current_authoritative_sha,
            "relationship": self.relationship,
            "validation_rerun_required": self.validation_rerun_required,
            "judgement_rerun_required": self.judgement_rerun_required,
            "reason_codes": _tuple_of_str(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class HandoffView:
    """NodeHandoff projection using repository-relative / opaque evidence refs only."""

    present: bool
    handoff_id: str | None
    source_node_id: str | None
    target_node_id: str | None
    cycle_id: str | None
    candidate_id: str | None
    evidence_refs_count: int
    readiness: bool | None
    missing_requirements: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "present": self.present,
            "handoff_id": self.handoff_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "cycle_id": self.cycle_id,
            "candidate_id": self.candidate_id,
            "evidence_refs_count": self.evidence_refs_count,
            "readiness": self.readiness,
            "missing_requirements": _tuple_of_str(self.missing_requirements),
        }


# --------------------------------------------------------------------------
# Barriers / blockers / events / artifacts / production serialization
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BarrierView:
    """Barrier explanation: not only ready/not-ready, but WHY (machine reasons)."""

    barrier_name: str
    ready: bool
    reason_codes: tuple[str, ...]
    missing_requirements: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "barrier_name": self.barrier_name,
            "ready": self.ready,
            "reason_codes": _tuple_of_str(self.reason_codes),
            "missing_requirements": _tuple_of_str(self.missing_requirements),
        }


@dataclass(frozen=True, slots=True)
class BlockerView:
    """Aggregated canonical blocker projection. Never cleared or suppressed here."""

    code: str
    scope: str
    affected_identity: str
    source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "scope": self.scope,
            "affected_identity": self.affected_identity,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class EventTimelineEntry:
    """Deterministic operator event timeline entry."""

    event_id: str
    event_type: str
    cycle_id: str
    task_id: str
    node_id: str
    execution_epoch: int
    source_kind: str
    source_id: str
    created_at: str
    run_id: str | None
    workspace_id: str | None
    candidate_id: str | None
    execution_host_id: str | None
    status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "cycle_id": self.cycle_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "execution_epoch": self.execution_epoch,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "created_at": self.created_at,
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "candidate_id": self.candidate_id,
            "execution_host_id": self.execution_host_id,
            "status": self.status,
        }

    @property
    def sort_key(self) -> tuple[str, str]:
        return (self.created_at, self.event_id)


@dataclass(frozen=True, slots=True)
class ArtifactView:
    """Snapshot / diff-artifact projection. Never reads filesystem artifacts."""

    artifact_id: str
    kind: str
    workspace_id: str
    candidate_id: str | None
    phase: str | None
    base_sha: str
    head_sha: str
    digest: str
    changed_path_count: int
    recorded_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "workspace_id": self.workspace_id,
            "candidate_id": self.candidate_id,
            "phase": self.phase,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "digest": self.digest,
            "changed_path_count": self.changed_path_count,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True, slots=True)
class ProductionSerializationView:
    """Production serialization barrier visibility. Assigns no owner."""

    active_mutation_agents: int
    owner_count: int
    production_owner: str | None
    ready: bool
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "active_mutation_agents": self.active_mutation_agents,
            "owner_count": self.owner_count,
            "production_owner": self.production_owner,
            "ready": self.ready,
            "reason_codes": _tuple_of_str(self.reason_codes),
        }


@dataclass(frozen=True, slots=True)
class RedactionRecord:
    """Disclosed redaction record. The original value is never retained."""

    field_path: str
    code: str = "OBSERVABILITY_REDACTION_REQUIRED"
    disclosure: str = "SUPPRESSED"

    def to_dict(self) -> dict[str, object]:
        return {
            "field_path": self.field_path,
            "code": self.code,
            "disclosure": self.disclosure,
        }
