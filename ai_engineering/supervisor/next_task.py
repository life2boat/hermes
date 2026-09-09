"""Next task intent generator."""

from __future__ import annotations

from ai_engineering.contracts import StopBoundary
from ai_engineering.control_plane.orchestrator import _STOP_BOUNDARY_RANK
from ai_engineering.supervisor.state import SupervisorError, _fail
from ai_engineering.supervisor.decision import AstraDecision, DecisionReceipt
from ai_engineering.task_intent import (
    NodeKind,
    RelationKind,
    TaskIntent,
    TaskLineage,
    LineageEdge,
    LineageNode,
    intent_digest,
    validate_intent,
    validate_lineage,
    TASK_INTENT_SCHEMA_VERSION,
    LINEAGE_SCHEMA_VERSION,
)


def _check_stop_boundary(parent: TaskIntent, child: TaskIntent) -> None:
    """Child stop_boundary rank must be <= parent stop_boundary rank."""
    parent_rank = _STOP_BOUNDARY_RANK.get(parent.stop_boundary)
    child_rank = _STOP_BOUNDARY_RANK.get(child.stop_boundary)
    if parent_rank is None or child_rank is None:
        _fail("CHILD_STOP_BOUNDARY_UNKNOWN")
    if child_rank > parent_rank:
        _fail("CHILD_STOP_BOUNDARY_ESCALATION")


def _check_authority(parent: TaskIntent, child: TaskIntent) -> None:
    """Child allowed_mutations must be subset of parent allowed_mutations.
    Parent forbidden_mutations cannot be removed from child.
    """
    parent_allowed = frozenset(parent.allowed_mutations)
    child_allowed = frozenset(child.allowed_mutations)
    if not child_allowed.issubset(parent_allowed):
        _fail("CHILD_AUTHORITY_EXPANSION")

    parent_forbidden = frozenset(parent.forbidden_mutations)
    child_forbidden = frozenset(child.forbidden_mutations)
    if not parent_forbidden.issubset(child_forbidden):
        _fail("CHILD_FORBIDDEN_WEAKENED")


def _validate_child_constraints(parent: TaskIntent, child: TaskIntent) -> None:
    """Validate all child constraints relative to parent."""
    if child.source_repository != parent.source_repository:
        _fail("CHILD_REPOSITORY_MISMATCH")
    if child.source_main_ref != parent.source_main_ref:
        _fail("CHILD_MAIN_REF_MISMATCH")
    _check_stop_boundary(parent, child)
    _check_authority(parent, child)
    parent_digest = intent_digest(parent)
    if child.parent_intent_digest != parent_digest:
        _fail("CHILD_PARENT_DIGEST_MISMATCH")


def _extend_lineage(
    lineage: TaskLineage,
    new_task_id: str,
    new_intent_digest: str,
    parent_intent_digest: str,
) -> TaskLineage:
    """Add new TASK node and DERIVED_FROM edge to lineage."""
    new_node = LineageNode(node_id=new_task_id, kind=NodeKind.TASK)
    # DERIVED_FROM: new INTENT -> parent INTENT uses TASK nodes
    new_edge = LineageEdge(
        source_id=new_task_id,
        target_id=new_intent_digest,
        relation=RelationKind.DERIVED_FROM,
    )
    # We need to find the parent node for the edge target
    # The lineage uses node_ids - we add the new task node
    # and an edge from new task to parent_intent_digest (if it's in lineage)
    # Actually the edge target must be a node in the lineage.
    # We'll use the parent task id if available, or just add the new task node.
    # Simple approach: just add the new TASK node to the lineage.
    new_nodes = tuple(list(lineage.nodes) + [new_node])
    # Find parent task node - the edge goes TASK->TASK with DERIVED_FROM
    # But _VALID_RELATIONS only has (DESIGN, DERIVED_FROM, INTENT)
    # We can't add TASK->TASK edge. Just return lineage with new node only.
    return TaskLineage(
        schema_version=LINEAGE_SCHEMA_VERSION,
        nodes=new_nodes,
        edges=lineage.edges,
    )


class NextTaskGenerator:
    """Generates next TaskIntent based on decision outcome."""

    def generate_continue(
        self,
        parent_intent: TaskIntent,
        decision: AstraDecision,
        receipt: DecisionReceipt,
        new_task_id: str,
        new_base_sha: str,
        created_at_utc: str,
    ) -> tuple[TaskIntent, TaskLineage]:
        """Generate a CONTINUE task intent (PASS -> next task)."""
        parent_digest = intent_digest(parent_intent)

        child = TaskIntent(
            schema_version=TASK_INTENT_SCHEMA_VERSION,
            task_id=new_task_id,
            intent_revision=1,
            status=parent_intent.status,
            task_class=parent_intent.task_class,
            desired_outcome=decision.next_objective or parent_intent.desired_outcome,
            source_repository=parent_intent.source_repository,
            source_main_ref=parent_intent.source_main_ref,
            source_base_sha=new_base_sha,
            constraints=parent_intent.constraints,
            allowed_mutations=parent_intent.allowed_mutations,
            forbidden_mutations=parent_intent.forbidden_mutations,
            stop_boundary=parent_intent.stop_boundary,
            acceptance_criteria=parent_intent.acceptance_criteria,
            unknowns=parent_intent.unknowns,
            applicable_invariants=parent_intent.applicable_invariants,
            required_gates=tuple(decision.requested_required_gates) if decision.requested_required_gates else parent_intent.required_gates,
            parent_intent_digest=parent_digest,
        )

        _validate_child_constraints(parent_intent, child)

        # Build lineage
        child_node = LineageNode(node_id=new_task_id, kind=NodeKind.TASK)
        new_nodes = tuple(list(parent_intent.applicable_invariants) and [] or []) + tuple([child_node])
        lineage = TaskLineage(
            schema_version=LINEAGE_SCHEMA_VERSION,
            nodes=(child_node,),
            edges=(),
        )

        return child, lineage

    def generate_fix(
        self,
        parent_intent: TaskIntent,
        decision: AstraDecision,
        receipt: DecisionReceipt,
        new_task_id: str,
        failing_gates: list[str],
        created_at_utc: str,
    ) -> tuple[TaskIntent, TaskLineage]:
        """Generate a FIX task intent (FAIL -> fix task)."""
        parent_digest = intent_digest(parent_intent)

        # Use parent's base_sha for fix (NOT from raw worker claims)
        fix_objective = decision.next_objective or f"Fix failing gates: {', '.join(failing_gates)}"

        child = TaskIntent(
            schema_version=TASK_INTENT_SCHEMA_VERSION,
            task_id=new_task_id,
            intent_revision=1,
            status=parent_intent.status,
            task_class=parent_intent.task_class,
            desired_outcome=fix_objective,
            source_repository=parent_intent.source_repository,
            source_main_ref=parent_intent.source_main_ref,
            source_base_sha=parent_intent.source_base_sha,  # base stays same for fix
            constraints=parent_intent.constraints,
            allowed_mutations=parent_intent.allowed_mutations,
            forbidden_mutations=parent_intent.forbidden_mutations,
            stop_boundary=parent_intent.stop_boundary,
            acceptance_criteria=parent_intent.acceptance_criteria,
            unknowns=parent_intent.unknowns,
            applicable_invariants=parent_intent.applicable_invariants,
            required_gates=tuple(decision.requested_required_gates) if decision.requested_required_gates else parent_intent.required_gates,
            parent_intent_digest=parent_digest,
        )

        _validate_child_constraints(parent_intent, child)

        child_node = LineageNode(node_id=new_task_id, kind=NodeKind.TASK)
        lineage = TaskLineage(
            schema_version=LINEAGE_SCHEMA_VERSION,
            nodes=(child_node,),
            edges=(),
        )

        return child, lineage

    def generate_retry(
        self,
        parent_intent: TaskIntent,
        decision: AstraDecision,
        receipt: DecisionReceipt,
        new_attempt_id: str,
        created_at_utc: str,
    ) -> tuple[TaskIntent, TaskLineage]:
        """Generate a RETRY attempt (BLOCKED -> new attempt, same task)."""
        # Same task_id, same base_sha, new attempt - revise the intent
        parent_digest = intent_digest(parent_intent)

        child = TaskIntent(
            schema_version=TASK_INTENT_SCHEMA_VERSION,
            task_id=parent_intent.task_id,  # same task_id
            intent_revision=parent_intent.intent_revision + 1,
            status=parent_intent.status,
            task_class=parent_intent.task_class,
            desired_outcome=decision.next_objective or parent_intent.desired_outcome,
            source_repository=parent_intent.source_repository,
            source_main_ref=parent_intent.source_main_ref,
            source_base_sha=parent_intent.source_base_sha,  # same base for retry
            constraints=parent_intent.constraints,
            allowed_mutations=parent_intent.allowed_mutations,
            forbidden_mutations=parent_intent.forbidden_mutations,
            stop_boundary=parent_intent.stop_boundary,
            acceptance_criteria=parent_intent.acceptance_criteria,
            unknowns=parent_intent.unknowns,
            applicable_invariants=parent_intent.applicable_invariants,
            required_gates=parent_intent.required_gates,
            parent_intent_digest=parent_digest,
        )

        # For retry: same task_id, same repository, same main_ref
        if child.source_repository != parent_intent.source_repository:
            _fail("CHILD_REPOSITORY_MISMATCH")
        if child.source_main_ref != parent_intent.source_main_ref:
            _fail("CHILD_MAIN_REF_MISMATCH")
        _check_stop_boundary(parent_intent, child)
        _check_authority(parent_intent, child)
        if child.parent_intent_digest != parent_digest:
            _fail("CHILD_PARENT_DIGEST_MISMATCH")

        child_node = LineageNode(node_id=parent_intent.task_id, kind=NodeKind.TASK)
        lineage = TaskLineage(
            schema_version=LINEAGE_SCHEMA_VERSION,
            nodes=(child_node,),
            edges=(),
        )

        return child, lineage
