from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import cast

from ai_engineering.graph_contract import (
    AuthoritativeFactState,
    AuthoritativeSourceSnapshot,
    GraphProvenance,
    GraphVerificationError,
)
from gateway.memory.graph_projection import (
    AuthoritativeMemoryFact,
    GraphProjectionResult,
    ProjectionError,
    read_authoritative_memory_facts,
)
from gateway.memory.graph_store import GraphStoreError, load_graph_projection


class GraphContextStatus(Enum):
    READY = auto()
    MISSING_GRAPH = auto()
    STALE_GRAPH = auto()


class GraphReadIntegrityError(Exception):
    """Persisted or hydrated graph evidence violated the read contract."""


@dataclass(frozen=True, slots=True)
class GraphFactQuery:
    entity: str | None = None
    key: str | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        if self.entity is not None and type(self.entity) is not str:
            raise ValueError("entity must be None or exact str")
        if self.key is not None and type(self.key) is not str:
            raise ValueError("key must be None or exact str")
        if type(self.limit) is not int or isinstance(self.limit, bool):
            raise ValueError("limit must be a strict int")
        if not (1 <= self.limit <= 100):
            raise ValueError("limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class GraphStructuralMatch:
    """Pure graph evidence; it deliberately contains no authoritative row."""

    fact_node_id: str
    entity: str
    key: str
    value: str
    node_provenance: GraphProvenance
    supports: tuple[GraphProvenance, ...]
    entity_node_id: str


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


def _integrity(code: str, cause: Exception | None = None) -> GraphReadIntegrityError:
    error = GraphReadIntegrityError(code)
    if cause is not None:
        error.__cause__ = cause
    return error


def _authoritative_source_snapshot(
    facts: tuple[AuthoritativeMemoryFact, ...],
) -> AuthoritativeSourceSnapshot:
    return AuthoritativeSourceSnapshot(
        tuple(
            AuthoritativeFactState(
                fact_id=str(fact.sqlite_id),
                current_revision=fact.vector_revision,
                status="ACTIVE",
            )
            for fact in facts
        ),
        is_complete=True,
    )


def _exact_properties(
    properties: object,
    *,
    expected_keys: frozenset[str],
    user_id: int,
    string_keys: tuple[str, ...] = (),
) -> None:
    if not isinstance(properties, Mapping):
        raise _integrity("STRUCTURAL_PROPERTIES_INVALID")
    if frozenset(properties) != expected_keys:
        raise _integrity("STRUCTURAL_PROPERTIES_INVALID")
    typed_properties = cast(Mapping[str, object], properties)
    if type(typed_properties["user_id"]) is not int or typed_properties["user_id"] != user_id:
        raise _integrity("STRUCTURAL_USER_ID_INVALID")
    for key in string_keys:
        if type(typed_properties[key]) is not str:
            raise _integrity("STRUCTURAL_PROPERTY_TYPE_INVALID")


def query_graph_projection(
    projection: GraphProjectionResult,
    query: GraphFactQuery,
) -> tuple[GraphStructuralMatch, ...]:
    """Pure Layer A: validate and query a verified in-memory graph projection."""

    if type(projection) is not GraphProjectionResult:
        raise ValueError("projection must be GraphProjectionResult")
    if not isinstance(query, GraphFactQuery):
        raise ValueError("query must be GraphFactQuery")
    if type(projection.user_id) is not int or isinstance(projection.user_id, bool):
        raise _integrity("PROJECTION_USER_ID_INVALID")

    try:
        projection.snapshot.verify_identity()
    except (GraphVerificationError, ValueError, TypeError) as exc:
        raise _integrity("SNAPSHOT_IDENTITY_INVALID", exc)

    nodes = {node.node_id: node for node in projection.snapshot.nodes}
    edges = projection.snapshot.edges
    if len(nodes) != len(projection.snapshot.nodes):
        raise _integrity("DUPLICATE_NODE_ID")

    if not nodes:
        if edges or projection.node_supports or projection.edge_supports:
            raise _integrity("EMPTY_GRAPH_STRUCTURE_INVALID")
        return ()

    allowed_node_types = {"memory:user", "memory:entity", "memory:fact"}
    if any(node.node_type not in allowed_node_types for node in nodes.values()):
        raise _integrity("UNEXPECTED_NODE_TYPE")

    user_nodes = [node for node in nodes.values() if node.node_type == "memory:user"]
    entity_nodes = {
        node.node_id: node
        for node in nodes.values()
        if node.node_type == "memory:entity"
    }
    fact_nodes = {
        node.node_id: node
        for node in nodes.values()
        if node.node_type == "memory:fact"
    }
    if len(user_nodes) != 1 or not entity_nodes or not fact_nodes:
        raise _integrity("STRUCTURAL_ROOT_INVALID")

    user_node = user_nodes[0]
    _exact_properties(
        user_node.properties,
        expected_keys=frozenset({"user_id"}),
        user_id=projection.user_id,
    )
    for entity_node in entity_nodes.values():
        _exact_properties(
            entity_node.properties,
            expected_keys=frozenset({"user_id", "entity"}),
            user_id=projection.user_id,
            string_keys=("entity",),
        )
    for fact_node in fact_nodes.values():
        _exact_properties(
            fact_node.properties,
            expected_keys=frozenset({"user_id", "entity", "key", "value"}),
            user_id=projection.user_id,
            string_keys=("entity", "key", "value"),
        )

    entity_parents: dict[str, list[str]] = {node_id: [] for node_id in entity_nodes}
    fact_parents: dict[str, list[str]] = {node_id: [] for node_id in fact_nodes}
    if set(projection.node_supports) != set(nodes):
        raise _integrity("NODE_SUPPORT_COVERAGE_INVALID")
    if set(projection.edge_supports) != {edge.edge_id for edge in edges}:
        raise _integrity("EDGE_SUPPORT_COVERAGE_INVALID")

    for edge in edges:
        if edge.relation_type == "memory:has_entity":
            if (
                edge.source_node_id != user_node.node_id
                or edge.target_node_id not in entity_nodes
            ):
                raise _integrity("HAS_ENTITY_PATH_INVALID")
            entity_parents[edge.target_node_id].append(edge.source_node_id)
        elif edge.relation_type == "memory:has_fact":
            if (
                edge.source_node_id not in entity_nodes
                or edge.target_node_id not in fact_nodes
            ):
                raise _integrity("HAS_FACT_PATH_INVALID")
            fact_parents[edge.target_node_id].append(edge.source_node_id)
        else:
            raise _integrity("UNEXPECTED_RELATION_TYPE")

    if any(parents != [user_node.node_id] for parents in entity_parents.values()):
        raise _integrity("ENTITY_REACHABILITY_INVALID")
    if any(len(parents) != 1 for parents in fact_parents.values()):
        raise _integrity("FACT_PARENT_CARDINALITY_INVALID")
    used_entities = {parents[0] for parents in fact_parents.values()}
    if used_entities != set(entity_nodes):
        raise _integrity("ENTITY_USAGE_INVALID")

    structural: list[GraphStructuralMatch] = []
    for fact_node in fact_nodes.values():
        entity_node_id = fact_parents[fact_node.node_id][0]
        entity_node = entity_nodes[entity_node_id]
        entity = fact_node.properties["entity"]
        key = fact_node.properties["key"]
        value = fact_node.properties["value"]
        if entity_node.properties["entity"] != entity:
            raise _integrity("ENTITY_LINKAGE_MISMATCH")

        supports = projection.node_supports.get(fact_node.node_id)
        if type(supports) is not tuple or not supports:
            raise _integrity("FACT_SUPPORTS_MISSING")
        if any(type(item) is not GraphProvenance for item in supports):
            raise _integrity("FACT_SUPPORT_INVALID")
        ordered_supports = tuple(
            sorted(
                supports,
                key=lambda item: (item.source_system, item.fact_id, item.revision),
            )
        )
        if fact_node.provenance not in ordered_supports:
            raise _integrity("PRIMARY_PROVENANCE_MISSING")
        structural.append(
            GraphStructuralMatch(
                fact_node_id=fact_node.node_id,
                entity=entity,
                key=key,
                value=value,
                node_provenance=fact_node.provenance,
                supports=ordered_supports,
                entity_node_id=entity_node_id,
            )
        )

    structural.sort(
        key=lambda item: (item.entity, item.key, item.value, item.fact_node_id)
    )
    filtered = (
        item
        for item in structural
        if (query.entity is None or item.entity == query.entity)
        and (query.key is None or item.key == query.key)
    )
    return tuple(filtered)[: query.limit]


def hydrate_graph_matches(
    current_facts: tuple[AuthoritativeMemoryFact, ...],
    structural_matches: tuple[GraphStructuralMatch, ...],
    *,
    user_id: int,
) -> tuple[GraphQueryMatch, ...]:
    """Layer B: bind structural matches to exact current authoritative rows."""

    if type(user_id) is not int or isinstance(user_id, bool):
        raise ValueError("user_id must be int")
    rows_by_id: dict[str, AuthoritativeMemoryFact] = {}
    for row in current_facts:
        if type(row) is not AuthoritativeMemoryFact:
            raise _integrity("AUTHORITATIVE_ROW_INVALID")
        row_id = str(row.sqlite_id)
        if row_id in rows_by_id:
            raise _integrity("AUTHORITATIVE_ROW_DUPLICATE")
        rows_by_id[row_id] = row

    hydrated: list[GraphQueryMatch] = []
    for match in structural_matches:
        if type(match) is not GraphStructuralMatch:
            raise _integrity("STRUCTURAL_MATCH_INVALID")
        if not match.supports or match.node_provenance not in match.supports:
            raise _integrity("STRUCTURAL_SUPPORT_INVALID")

        support_rows: list[AuthoritativeMemoryFact] = []
        seen: set[tuple[str, int]] = set()
        for provenance in match.supports:
            if provenance.source_system != "sqlite_memory_os_facts":
                raise _integrity("UNKNOWN_SOURCE_SYSTEM")
            if re.fullmatch(r"[1-9][0-9]*", provenance.fact_id) is None:
                raise _integrity("NONCANONICAL_PROVENANCE_FACT_ID")
            support_key = (provenance.fact_id, provenance.revision)
            if support_key in seen:
                continue
            seen.add(support_key)

            row = rows_by_id.get(provenance.fact_id)
            if row is None or str(row.sqlite_id) != provenance.fact_id:
                raise _integrity("SUPPORT_ROW_MISSING")
            if row.vector_revision != provenance.revision:
                raise _integrity("SUPPORT_REVISION_MISMATCH")
            if row.user_id != user_id:
                raise _integrity("SUPPORT_USER_MISMATCH")
            if row.entity != match.entity:
                raise _integrity("SUPPORT_ENTITY_MISMATCH")
            if row.key != match.key:
                raise _integrity("SUPPORT_KEY_MISMATCH")
            if row.value != match.value:
                raise _integrity("SUPPORT_VALUE_MISMATCH")
            support_rows.append(row)

        if not support_rows:
            raise _integrity("SUPPORT_ROWS_EMPTY")
        support_rows.sort(key=lambda row: (row.sqlite_id, row.vector_revision))
        hydrated.append(
            GraphQueryMatch(
                fact_node_id=match.fact_node_id,
                entity=match.entity,
                key=match.key,
                value=match.value,
                node_provenance=match.node_provenance,
                supports=tuple(support_rows),
                entity_node_id=match.entity_node_id,
            )
        )
    return tuple(hydrated)


def _status_result(
    *,
    status: GraphContextStatus,
    user_id: int,
    query: GraphFactQuery,
    projection: GraphProjectionResult | None,
) -> GraphContextResult:
    return GraphContextResult(
        status=status,
        user_id=user_id,
        snapshot_id=None if projection is None else projection.snapshot.snapshot_id,
        projection_id=None if projection is None else projection.projection_id,
        query=query,
        matches=(),
        matched_count=0,
    )


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

    current_facts = read_authoritative_memory_facts(conn, user_id=user_id)
    try:
        projection = load_graph_projection(conn, user_id=user_id)
    except (GraphStoreError, GraphVerificationError, ProjectionError) as exc:
        raise _integrity("PERSISTED_GRAPH_INVALID", exc)

    if projection is None:
        return _status_result(
            status=GraphContextStatus.MISSING_GRAPH,
            user_id=user_id,
            query=query,
            projection=None,
        )
    if projection.user_id != user_id:
        raise _integrity("PROJECTION_USER_MISMATCH")

    persisted_source = projection.snapshot.authoritative_source
    if persisted_source is None or not persisted_source.is_complete:
        raise _integrity("AUTHORITATIVE_SOURCE_INCOMPLETE")
    try:
        current_source = _authoritative_source_snapshot(current_facts)
    except (GraphVerificationError, ValueError, TypeError) as exc:
        raise _integrity("CURRENT_SOURCE_STATE_INVALID", exc)
    if current_source != persisted_source:
        return _status_result(
            status=GraphContextStatus.STALE_GRAPH,
            user_id=user_id,
            query=query,
            projection=projection,
        )

    structural_matches = query_graph_projection(projection, query)
    matches = hydrate_graph_matches(current_facts, structural_matches, user_id=user_id)
    return GraphContextResult(
        status=GraphContextStatus.READY,
        user_id=user_id,
        snapshot_id=projection.snapshot.snapshot_id,
        projection_id=projection.projection_id,
        query=query,
        matches=matches,
        matched_count=len(matches),
    )
