import sqlite3
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Tuple

from ai_engineering.graph_contract import GraphProvenance, GraphVerificationError
from gateway.memory.graph_projection import (
    AuthoritativeMemoryFact,
    read_authoritative_memory_facts,
    ProjectionError,
)
from gateway.memory.graph_store import load_graph_projection, GraphStoreError


class GraphContextStatus(Enum):
    READY = auto()
    MISSING_GRAPH = auto()
    STALE_GRAPH = auto()


class GraphReadIntegrityError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class GraphFactQuery:
    entity: str | None
    key: str | None
    limit: int = 50

    def __post_init__(self):
        if self.entity is not None and type(self.entity) is not str:
            raise ValueError("entity must be None or exact str")
        if self.key is not None and type(self.key) is not str:
            raise ValueError("key must be None or exact str")
        if type(self.limit) is not int or isinstance(self.limit, bool):
            raise ValueError("limit must be a strict int")
        if not (1 <= self.limit <= 100):
            raise ValueError("limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class GraphQueryMatch:
    fact_node_id: str
    entity: str
    key: str
    value: str
    node_provenance: GraphProvenance
    supports: tuple[AuthoritativeMemoryFact, ...]
    entity_node_id: str | None = None


@dataclass(frozen=True, slots=True)
class GraphContextResult:
    status: GraphContextStatus
    user_id: int
    snapshot_id: str | None
    projection_id: str | None
    query: GraphFactQuery
    matches: tuple[GraphQueryMatch, ...]
    matched_count: int


def read_graph_context(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    query: GraphFactQuery,
) -> GraphContextResult:
    if type(user_id) is not int or isinstance(user_id, bool):
        raise ValueError("user_id must be int")
    if not isinstance(query, GraphFactQuery):
        raise ValueError("query must be GraphFactQuery")

    # 1. Authoritative freshness check
    current_facts = read_authoritative_memory_facts(conn, user_id=user_id)

    # 2. Load projection (integrity gate)
    try:
        projection = load_graph_projection(conn, user_id=user_id)
    except (GraphStoreError, GraphVerificationError, ProjectionError) as e:
        raise GraphReadIntegrityError(f"Corrupted persisted graph: {e}") from e

    if projection is None:
        return GraphContextResult(
            status=GraphContextStatus.MISSING_GRAPH,
            user_id=user_id,
            snapshot_id=None,
            projection_id=None,
            query=query,
            matches=(),
            matched_count=0,
        )

    # 3. Freshness Gate
    auth_source = projection.snapshot.authoritative_source
    if len(current_facts) != len(auth_source.facts):
        return GraphContextResult(
            status=GraphContextStatus.STALE_GRAPH,
            user_id=user_id,
            snapshot_id=projection.snapshot.snapshot_id,
            projection_id=projection.projection_id,
            query=query,
            matches=(),
            matched_count=0,
        )

    # Fast equality check based on deterministic order of facts in current_facts and auth_source
    for c_fact, a_fact in zip(current_facts, auth_source.facts):
        if (
            str(c_fact.sqlite_id) != a_fact.fact_id
            or c_fact.vector_revision != a_fact.current_revision
            or a_fact.status != "ACTIVE"
        ):
            return GraphContextResult(
                status=GraphContextStatus.STALE_GRAPH,
                user_id=user_id,
                snapshot_id=projection.snapshot.snapshot_id,
                projection_id=projection.projection_id,
                query=query,
                matches=(),
                matched_count=0,
            )

    # 4. Pure query layer on snapshot
    matches = []
    auth_dict = {f.sqlite_id: f for f in current_facts}

    entity_nodes = {
        n.node_id: n
        for n in projection.snapshot.nodes
        if n.node_type == "memory:entity"
    }

    has_fact_edges = [
        e for e in projection.snapshot.edges if e.relation_type == "memory:has_fact"
    ]
    valid_fact_node_ids = set()
    fact_to_entity_map = {}
    for e in has_fact_edges:
        if e.source_node_id in entity_nodes:
            valid_fact_node_ids.add(e.target_node_id)
            fact_to_entity_map[e.target_node_id] = e.source_node_id

    for n in projection.snapshot.nodes:
        if n.node_type != "memory:fact":
            continue
        if n.node_id not in valid_fact_node_ids:
            continue

        entity = n.properties.get("entity")
        key = n.properties.get("key")
        value = n.properties.get("value")

        if type(entity) is not str or type(key) is not str or type(value) is not str:
            continue

        ent_node = entity_nodes.get(fact_to_entity_map[n.node_id])
        if not ent_node or ent_node.properties.get("entity") != entity:
            continue

        if query.entity is not None and entity != query.entity:
            continue

        if query.key is not None and key != query.key:
            continue

        node_supports = projection.node_supports.get(n.node_id, ())
        if not node_supports:
            raise GraphReadIntegrityError(f"Missing node supports for {n.node_id}")

        # 5. Authoritative Hydration
        hydrated_supports = []
        # Multi-support deduplication: we gather all supports into a single tuple
        seen_supports = set()
        for prov in node_supports:
            if prov.source_system != "sqlite_memory_os_facts":
                raise GraphReadIntegrityError(
                    f"Unknown source system {prov.source_system}"
                )

            fact_id = int(prov.fact_id)
            if fact_id not in auth_dict:
                raise GraphReadIntegrityError(
                    f"Missing support row for fact_id {fact_id}"
                )

            row = auth_dict[fact_id]
            if row.vector_revision != prov.revision:
                raise GraphReadIntegrityError(
                    f"Revision mismatch for support fact_id {fact_id}"
                )
            if row.user_id != user_id:
                raise GraphReadIntegrityError(
                    f"User mismatch for support fact_id {fact_id}"
                )
            if row.entity != entity or row.key != key or row.value != value:
                raise GraphReadIntegrityError(
                    f"Semantic mismatch for support fact_id {fact_id}"
                )

            support_key = (row.sqlite_id, row.vector_revision)
            if support_key not in seen_supports:
                seen_supports.add(support_key)
                hydrated_supports.append(row)

        if not hydrated_supports:
            raise GraphReadIntegrityError(f"No valid supports hydrated for {n.node_id}")

        hydrated_supports.sort(key=lambda f: (f.sqlite_id, f.vector_revision))

        matches.append((
            entity,
            key,
            value,
            n.node_id,
            n.provenance,
            tuple(hydrated_supports),
            ent_node.node_id,
        ))

    # Deterministic sorting
    matches.sort(key=lambda m: (m[0], m[1], m[2], m[3]))

    # Apply limit
    matches = matches[: query.limit]

    query_matches = tuple(
        GraphQueryMatch(
            fact_node_id=m[3],
            entity=m[0],
            key=m[1],
            value=m[2],
            node_provenance=m[4],
            supports=m[5],
            entity_node_id=m[6],
        )
        for m in matches
    )

    return GraphContextResult(
        status=GraphContextStatus.READY,
        user_id=user_id,
        snapshot_id=projection.snapshot.snapshot_id,
        projection_id=projection.projection_id,
        query=query,
        matches=query_matches,
        matched_count=len(query_matches),
    )
