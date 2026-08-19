import sqlite3
from dataclasses import dataclass
from enum import Enum, auto

from ai_engineering.graph_contract import GraphVerificationError
from gateway.memory.graph_projection import (
    read_authoritative_memory_facts,
    project_authoritative_memory_facts,
    build_authoritative_source_snapshot,
    ProjectionError,
)
from gateway.memory.graph_store import (
    load_graph_projection,
    publish_graph_projection,
    validate_memory_graph_store_schema,
    GraphStoreError,
)


class GraphConvergenceState(Enum):
    CURRENT = "CURRENT"
    MISSING = "MISSING"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class GraphConvergenceAssessment:
    state: GraphConvergenceState
    matched_auth_facts_count: int


class GraphConvergenceStatus(Enum):
    NOOP_CURRENT = "NOOP_CURRENT"
    REBUILT_MISSING = "REBUILT_MISSING"
    REBUILT_STALE = "REBUILT_STALE"
    SOURCE_CHURN_RETRY_EXHAUSTED = "SOURCE_CHURN_RETRY_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class GraphConvergenceResult:
    status: GraphConvergenceStatus
    user_id: int
    attempts: int
    publish_count: int
    projection_id: str | None
    snapshot_id: str | None
    initial_state: GraphConvergenceState
    final_state: GraphConvergenceState


class GraphConvergenceError(Exception):
    pass


class GraphConvergenceIntegrityError(GraphConvergenceError):
    pass


def inspect_graph_convergence(
    conn: sqlite3.Connection, *, user_id: int
) -> GraphConvergenceAssessment:
    if type(user_id) is not int or isinstance(user_id, bool):
        raise ValueError("user_id must be int")

    facts = read_authoritative_memory_facts(conn, user_id=user_id)
    try:
        projection = load_graph_projection(conn, user_id=user_id)
    except (GraphStoreError, GraphVerificationError, ProjectionError) as e:
        raise GraphConvergenceIntegrityError(f"Corrupted persisted graph: {e}") from e

    if projection is None:
        return GraphConvergenceAssessment(
            state=GraphConvergenceState.MISSING,
            matched_auth_facts_count=len(facts),
        )

    current_source = build_authoritative_source_snapshot(facts)
    persisted_source = projection.snapshot.authoritative_source

    if not persisted_source.is_complete:
        raise GraphConvergenceIntegrityError("Persisted source is incomplete")

    if current_source != persisted_source:
        return GraphConvergenceAssessment(
            state=GraphConvergenceState.STALE,
            matched_auth_facts_count=len(facts),
        )

    return GraphConvergenceAssessment(
        state=GraphConvergenceState.CURRENT,
        matched_auth_facts_count=len(facts),
    )


def converge_user_graph(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    max_attempts: int = 3,
) -> GraphConvergenceResult:
    if type(user_id) is not int or isinstance(user_id, bool):
        raise ValueError("user_id must be int")
    if type(max_attempts) is not int or isinstance(max_attempts, bool):
        raise ValueError("max_attempts must be int")
    if not (1 <= max_attempts <= 5):
        raise ValueError("max_attempts must be between 1 and 5")

    assessment = inspect_graph_convergence(conn, user_id=user_id)
    initial_state = assessment.state

    if initial_state == GraphConvergenceState.CURRENT:
        projection = load_graph_projection(conn, user_id=user_id)
        return GraphConvergenceResult(
            status=GraphConvergenceStatus.NOOP_CURRENT,
            user_id=user_id,
            attempts=0,
            publish_count=0,
            projection_id=projection.projection_id if projection else None,
            snapshot_id=projection.snapshot.snapshot_id if projection else None,
            initial_state=initial_state,
            final_state=GraphConvergenceState.CURRENT,
        )

    validate_memory_graph_store_schema(conn)

    publish_count = 0
    for attempt in range(1, max_attempts + 1):
        facts_a = read_authoritative_memory_facts(conn, user_id=user_id)
        source_a = build_authoritative_source_snapshot(facts_a)
        
        candidate = project_authoritative_memory_facts(facts_a, user_id=user_id)
        
        facts_b = read_authoritative_memory_facts(conn, user_id=user_id)
        source_b = build_authoritative_source_snapshot(facts_b)
        
        if source_a != source_b:
            continue

        publish_graph_projection(conn, candidate)
        publish_count += 1

        try:
            loaded = load_graph_projection(conn, user_id=user_id)
        except (GraphStoreError, GraphVerificationError, ProjectionError) as e:
            raise GraphConvergenceIntegrityError("Corrupted persisted graph after publish") from e
            
        if loaded is None:
            raise GraphConvergenceIntegrityError("Graph missing immediately after publish")

        facts_c = read_authoritative_memory_facts(conn, user_id=user_id)
        source_c = build_authoritative_source_snapshot(facts_c)

        if candidate.snapshot.authoritative_source != loaded.snapshot.authoritative_source:
            raise GraphConvergenceIntegrityError("Persisted source corrupted upon load")

        if loaded.snapshot.authoritative_source == source_c:
            status = GraphConvergenceStatus.REBUILT_MISSING if initial_state == GraphConvergenceState.MISSING else GraphConvergenceStatus.REBUILT_STALE
            return GraphConvergenceResult(
                status=status,
                user_id=user_id,
                attempts=attempt,
                publish_count=publish_count,
                projection_id=loaded.projection_id,
                snapshot_id=loaded.snapshot.snapshot_id,
                initial_state=initial_state,
                final_state=GraphConvergenceState.CURRENT,
            )

    # Churn exhaustion
    final_assessment = inspect_graph_convergence(conn, user_id=user_id)
    return GraphConvergenceResult(
        status=GraphConvergenceStatus.SOURCE_CHURN_RETRY_EXHAUSTED,
        user_id=user_id,
        attempts=max_attempts,
        publish_count=publish_count,
        projection_id=None,
        snapshot_id=None,
        initial_state=initial_state,
        final_state=final_assessment.state,
    )
