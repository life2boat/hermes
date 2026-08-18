from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping
from types import MappingProxyType
import math

from ai_engineering.graph_contract import (
    GraphNode, GraphEdge, GraphProvenance, GraphSnapshot,
    AuthoritativeSourceSnapshot, AuthoritativeFactState,
    MAX_GRAPH_NODES, MAX_GRAPH_EDGES, MAX_PROPERTIES_PER_ENTITY,
    MAX_PROPERTY_KEY_LENGTH, MAX_STRING_PROPERTY_LENGTH, GraphVerificationError
)
from gateway.memory.schema import FACTS_TABLE, validate_memory_convergence_schema

MEMORY_GRAPH_PROJECTION_VERSION = 1
MAX_PROJECTION_FACTS = 499

class ProjectionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AuthoritativeMemoryFact:
    sqlite_id: int
    user_id: int
    entity: str
    key: str
    value: str
    vector_revision: int
    source: str | None
    trust_score: float
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if type(self.sqlite_id) is not int or isinstance(self.sqlite_id, bool) or self.sqlite_id <= 0:
            raise ValueError("sqlite_id must be a positive int")
        if type(self.user_id) is not int or isinstance(self.user_id, bool):
            raise ValueError("user_id must be int")
        if type(self.entity) is not str: raise ValueError("entity must be str")
        if type(self.key) is not str: raise ValueError("key must be str")
        if type(self.value) is not str: raise ValueError("value must be str")
        if type(self.vector_revision) is not int or isinstance(self.vector_revision, bool) or self.vector_revision < 1:
            raise ValueError("vector_revision must be int >= 1")
        if self.source is not None and type(self.source) is not str:
            raise ValueError("source must be None or str")
        if type(self.trust_score) not in (float, int) or isinstance(self.trust_score, bool) or not math.isfinite(self.trust_score):
            raise ValueError("trust_score must be a finite real number")
        if type(self.created_at) is not str: raise ValueError("created_at must be str")
        if type(self.updated_at) is not str: raise ValueError("updated_at must be str")


@dataclass(frozen=True, slots=True)
class ProjectionExclusion:
    fact_id: int
    reason: str


@dataclass(frozen=True, slots=True)
class GraphProjectionResult:
    projection_version: int
    user_id: int
    snapshot: GraphSnapshot
    projection_id: str
    input_fact_count: int
    projected_fact_count: int
    excluded_fact_count: int
    node_supports: MappingProxyType[str, tuple[GraphProvenance, ...]]
    edge_supports: MappingProxyType[str, tuple[GraphProvenance, ...]]
    exclusions: tuple[ProjectionExclusion, ...]


def read_authoritative_memory_facts(
    conn: sqlite3.Connection,
    *,
    user_id: int,
) -> tuple[AuthoritativeMemoryFact, ...]:
    if type(user_id) is not int or isinstance(user_id, bool):
        raise ValueError("user_id must be int")

    validate_memory_convergence_schema(conn)

    rows = conn.execute(
        f"SELECT id, user_id, entity, key, value, vector_revision, source, trust_score, created_at, updated_at "
        f"FROM {FACTS_TABLE} WHERE user_id = ? ORDER BY id ASC",
        (user_id,)
    ).fetchall()

    facts = []
    for row in rows:
        facts.append(AuthoritativeMemoryFact(
            sqlite_id=row[0],
            user_id=row[1],
            entity=row[2],
            key=row[3],
            value=row[4],
            vector_revision=row[5],
            source=row[6],
            trust_score=row[7],
            created_at=row[8],
            updated_at=row[9],
        ))
    return tuple(facts)


def project_authoritative_memory_facts(
    facts: tuple[AuthoritativeMemoryFact, ...],
    *,
    user_id: int,
) -> GraphProjectionResult:
    if type(user_id) is not int or isinstance(user_id, bool):
        raise ProjectionError("user_id must be int")
    if len(facts) > MAX_PROJECTION_FACTS:
        raise ProjectionError(f"PROJECTION_LIMIT_EXCEEDED: facts count {len(facts)} > {MAX_PROJECTION_FACTS}")

    sqlite_ids = set()
    for f in facts:
        if type(f) is not AuthoritativeMemoryFact:
            raise ProjectionError("fact must be AuthoritativeMemoryFact")
        if f.user_id != user_id:
            raise ProjectionError(f"CROSS_USER_INPUT_REJECTION: expected user {user_id}, got {f.user_id}")
        if f.sqlite_id in sqlite_ids:
            raise ProjectionError(f"Duplicate sqlite_id {f.sqlite_id}")
        sqlite_ids.add(f.sqlite_id)

    # Sort input to ensure deterministic order (id ASC)
    sorted_facts = sorted(facts, key=lambda x: x.sqlite_id)

    # State tracking
    exclusions = []
    node_supports: dict[str, list[GraphProvenance]] = {}
    edge_supports: dict[str, list[GraphProvenance]] = {}

    # Semantic uniqueness dictionaries.
    # Key -> Node properties dictionary proxy or tuple representing identity
    semantic_user_node = None
    semantic_entities: dict[str, GraphNode] = {}
    semantic_facts: dict[tuple[str, str, str], GraphNode] = {}

    semantic_has_entity: dict[str, GraphEdge] = {}
    semantic_has_fact: dict[tuple[str, str, str], GraphEdge] = {}

    prohibited_keys = {"raw_prompt", "compiled_prompt", "chain_of_thought", "cot", "credential",
                       "password", "api_key", "secret", "access_token", "refresh_token", "provider_raw_response"}

    # Authoritative source snapshot
    auth_fact_states = []
    for f in sorted_facts:
        auth_fact_states.append(AuthoritativeFactState(
            fact_id=str(f.sqlite_id),
            current_revision=f.vector_revision,
            status="ACTIVE"
        ))
    auth_source = AuthoritativeSourceSnapshot(tuple(auth_fact_states), is_complete=True)

    projected_fact_count = 0
    excluded_fact_count = 0

    # Build structural graph
    for f in sorted_facts:
        k_lower = f.key.lower()
        if k_lower in prohibited_keys:
            exclusions.append(ProjectionExclusion(f.sqlite_id, "PROHIBITED_FIELD"))
            excluded_fact_count += 1
            continue

        # Check property constraints
        if len(f.key) > MAX_STRING_PROPERTY_LENGTH:
            exclusions.append(ProjectionExclusion(f.sqlite_id, "GRAPH_STRING_BOUND_EXCEEDED"))
            excluded_fact_count += 1
            continue
        if len(f.value) > MAX_STRING_PROPERTY_LENGTH:
            exclusions.append(ProjectionExclusion(f.sqlite_id, "GRAPH_STRING_BOUND_EXCEEDED"))
            excluded_fact_count += 1
            continue
        if len(f.entity) > MAX_STRING_PROPERTY_LENGTH:
            exclusions.append(ProjectionExclusion(f.sqlite_id, "GRAPH_STRING_BOUND_EXCEEDED"))
            excluded_fact_count += 1
            continue

        prov = GraphProvenance("sqlite_memory_os_facts", str(f.sqlite_id), f.vector_revision)

        # User node
        if semantic_user_node is None:
            semantic_user_node = GraphNode.create("memory:user", {"user_id": user_id}, prov)
            node_supports[semantic_user_node.node_id] = []
        node_supports[semantic_user_node.node_id].append(prov)

        # Entity node
        if f.entity not in semantic_entities:
            ent_node = GraphNode.create("memory:entity", {"user_id": user_id, "entity": f.entity}, prov)
            semantic_entities[f.entity] = ent_node
            node_supports[ent_node.node_id] = []

            ent_edge = GraphEdge.create(semantic_user_node.node_id, ent_node.node_id, "memory:has_entity", {}, prov)
            semantic_has_entity[f.entity] = ent_edge
            edge_supports[ent_edge.edge_id] = []

        ent_node = semantic_entities[f.entity]
        ent_edge = semantic_has_entity[f.entity]
        node_supports[ent_node.node_id].append(prov)
        edge_supports[ent_edge.edge_id].append(prov)

        # Fact node
        fact_key = (f.entity, f.key, f.value)
        if fact_key not in semantic_facts:
            fact_node = GraphNode.create("memory:fact", {"user_id": user_id, "entity": f.entity, "key": f.key, "value": f.value}, prov)
            semantic_facts[fact_key] = fact_node
            node_supports[fact_node.node_id] = []

            fact_edge = GraphEdge.create(ent_node.node_id, fact_node.node_id, "memory:has_fact", {}, prov)
            semantic_has_fact[fact_key] = fact_edge
            edge_supports[fact_edge.edge_id] = []

        fact_node = semantic_facts[fact_key]
        fact_edge = semantic_has_fact[fact_key]
        node_supports[fact_node.node_id].append(prov)
        edge_supports[fact_edge.edge_id].append(prov)

        projected_fact_count += 1

    nodes = []
    edges = []
    if semantic_user_node is not None:
        nodes.append(semantic_user_node)
    nodes.extend(semantic_entities.values())
    nodes.extend(semantic_facts.values())

    edges.extend(semantic_has_entity.values())
    edges.extend(semantic_has_fact.values())

    snapshot = GraphSnapshot.create(nodes, edges, auth_source)

    # Compute deterministic projection_id
    hasher = hashlib.sha256()
    hasher.update(f"v{MEMORY_GRAPH_PROJECTION_VERSION}:user:{user_id}:snapshot:{snapshot.snapshot_id}:".encode("utf-8"))

    hasher.update(f"auth_complete:{auth_source.is_complete}:".encode("utf-8"))
    for f_state in auth_source.facts:
        hasher.update(f"auth_fact:{f_state.fact_id}:{f_state.current_revision}:{f_state.status}:".encode("utf-8"))

    # Bind canonical evidence to projection_id
    # Sort nodes and edges for evidence binding
    ordered_node_ids = sorted(node_supports.keys())
    for nid in ordered_node_ids:
        hasher.update(f"n:{nid}:".encode("utf-8"))
        for p in sorted(node_supports[nid], key=lambda x: (int(x.fact_id), x.revision)):
            hasher.update(f"{p.fact_id}_{p.revision}:".encode("utf-8"))

    ordered_edge_ids = sorted(edge_supports.keys())
    for eid in ordered_edge_ids:
        hasher.update(f"e:{eid}:".encode("utf-8"))
        for p in sorted(edge_supports[eid], key=lambda x: (int(x.fact_id), x.revision)):
            hasher.update(f"{p.fact_id}_{p.revision}:".encode("utf-8"))

    for ex in sorted(exclusions, key=lambda x: x.fact_id):
        hasher.update(f"ex:{ex.fact_id}:{ex.reason}:".encode("utf-8"))

    projection_id = f"gp_{hasher.hexdigest()}"

    return GraphProjectionResult(
        projection_version=MEMORY_GRAPH_PROJECTION_VERSION,
        user_id=user_id,
        snapshot=snapshot,
        projection_id=projection_id,
        input_fact_count=len(facts),
        projected_fact_count=projected_fact_count,
        excluded_fact_count=excluded_fact_count,
        node_supports=MappingProxyType({k: tuple(sorted(v, key=lambda x: (int(x.fact_id), x.revision))) for k, v in node_supports.items()}),
        edge_supports=MappingProxyType({k: tuple(sorted(v, key=lambda x: (int(x.fact_id), x.revision))) for k, v in edge_supports.items()}),
        exclusions=tuple(sorted(exclusions, key=lambda x: x.fact_id))
    )
