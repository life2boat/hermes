import sqlite3
from dataclasses import dataclass
from enum import Enum, auto

from ai_engineering.graph_contract import GraphVerificationError
from gateway.memory.graph_projection import (
    read_authoritative_memory_facts,
    project_authoritative_memory_facts,
    ProjectionError,
)
from gateway.memory.graph_store import (
    load_graph_projection,
    publish_graph_projection,
    validate_memory_graph_store_schema,
    GraphStoreError,
)


class GraphConvergenceStatus(Enum):
    CURRENT_NOOP = auto()
    MISSING_REBUILD = auto()
    STALE_REBUILD = auto()


@dataclass(frozen=True, slots=True)
class GraphConvergenceResult:
    status: GraphConvergenceStatus
    user_id: int
    matched_auth_facts_count: int


class GraphConvergenceError(Exception):
    pass


def _is_stale(current_facts, projection) -> bool:
    if projection is None:
        return True
    auth_source = projection.snapshot.authoritative_source
    if len(current_facts) != len(auth_source.facts):
        return True
    for c_fact, a_fact in zip(current_facts, auth_source.facts):
        if (
            str(c_fact.sqlite_id) != a_fact.fact_id
            or c_fact.vector_revision != a_fact.current_revision
            or a_fact.status != "ACTIVE"
        ):
            return True
    return False


def converge_user_graph(
    conn: sqlite3.Connection, user_id: int
) -> GraphConvergenceResult:
    if type(user_id) is not int or isinstance(user_id, bool):
        raise ValueError("user_id must be int")

    # 1. Inspect current
    current_facts = read_authoritative_memory_facts(conn, user_id=user_id)
    try:
        projection = load_graph_projection(conn, user_id=user_id)
    except (GraphStoreError, GraphVerificationError, ProjectionError) as e:
        raise GraphConvergenceError(f"Corrupted persisted graph: {e}") from e

    if projection is None:
        status = GraphConvergenceStatus.MISSING_REBUILD
    elif _is_stale(current_facts, projection):
        status = GraphConvergenceStatus.STALE_REBUILD
    else:
        status = GraphConvergenceStatus.CURRENT_NOOP

    if status == GraphConvergenceStatus.CURRENT_NOOP:
        return GraphConvergenceResult(
            status=status,
            user_id=user_id,
            matched_auth_facts_count=len(current_facts),
        )

    # Rebuild logic
    validate_memory_graph_store_schema(conn)
    max_retries = 3
    for attempt in range(max_retries):
        # Read authoritative facts again for rebuild
        rebuild_facts = read_authoritative_memory_facts(conn, user_id=user_id)

        # PRE_PUBLISH_REVALIDATION
        # We must verify that authoritative facts haven't churned between our read and project.
        # But wait, SQLite transaction semantics mean within this connection, unless we release
        # a savepoint or commit, they won't change. But let's follow the requested structure.
        pre_publish_facts = read_authoritative_memory_facts(conn, user_id=user_id)
        if len(rebuild_facts) != len(pre_publish_facts) or any(
            f1.sqlite_id != f2.sqlite_id or f1.vector_revision != f2.vector_revision
            for f1, f2 in zip(rebuild_facts, pre_publish_facts)
        ):
            continue  # Churn detected before publish, retry

        new_projection = project_authoritative_memory_facts(
            pre_publish_facts, user_id=user_id
        )
        publish_graph_projection(conn, new_projection)

        # POST_PUBLISH_REVALIDATION
        post_publish_facts = read_authoritative_memory_facts(conn, user_id=user_id)
        try:
            saved_projection = load_graph_projection(conn, user_id=user_id)
        except Exception as e:
            raise GraphConvergenceError(
                "Corrupted persisted graph after publish"
            ) from e

        if saved_projection is None or _is_stale(post_publish_facts, saved_projection):
            continue  # Churn detected after publish, retry

        return GraphConvergenceResult(
            status=status,
            user_id=user_id,
            matched_auth_facts_count=len(post_publish_facts),
        )

    raise GraphConvergenceError(
        "SOURCE_CHURN_EXHAUSTION: Rebuild attempts exhausted due to fact churn."
    )
