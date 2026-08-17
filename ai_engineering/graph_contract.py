"""
Deterministic Graph Contract Foundation (v3)

This module provides the architectural and executable foundation for
the derived graph memory layer.

AUTHORITY BOUNDARY:
- SQLite `memory_os_facts` remains AUTHORITATIVE.
- Graph relations produced by this module are DERIVED_REBUILDABLE.
- GraphNode represents a semantic entity (ENTITY semantics).
- GraphEdge represents a specific semantic relation.
- Both nodes and edges are assertions bound to authoritative facts via GraphProvenance.
- Graph state can be safely deleted and rebuilt from SQLite facts.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

GRAPH_SCHEMA_VERSION = 1

# Boundedness
MAX_GRAPH_NODES = 1000
MAX_GRAPH_EDGES = 5000
MAX_PROPERTIES_PER_ENTITY = 100
MAX_PROPERTY_KEY_LENGTH = 100
MAX_STRING_PROPERTY_LENGTH = 4096


class GraphVerificationError(ValueError):
    """Raised when graph assertions fail determinism, validation, or tamper checks."""
    pass


def _validate_type_string(val: str, field_name: str) -> None:
    if not isinstance(val, str) or not re.match(r"^[a-z0-9_]+:[a-z0-9_]+$", val):
        raise GraphVerificationError(f"Invalid {field_name}: '{val}'. Must match ^[a-z0-9_]+:[a-z0-9_]+$")


def _reject_secrets_and_validate_properties(properties: Mapping[str, Any]) -> MappingProxyType[str, Any]:
    if not isinstance(properties, Mapping):
        raise GraphVerificationError("properties must be a Mapping")
    if len(properties) > MAX_PROPERTIES_PER_ENTITY:
        raise GraphVerificationError(f"properties exceed MAX_PROPERTIES_PER_ENTITY ({MAX_PROPERTIES_PER_ENTITY})")

    blocked_fragments = {"raw_prompt", "compiled_prompt", "chain_of_thought", "cot", "credential", 
                         "password", "api_key", "secret", "access_token", "refresh_token", "provider_raw_response"}
    
    clean_props = {}
    for key, val in properties.items():
        if not isinstance(key, str):
            raise GraphVerificationError("Property keys must be strings")
        if len(key) > MAX_PROPERTY_KEY_LENGTH:
            raise GraphVerificationError(f"Property key length exceeds MAX_PROPERTY_KEY_LENGTH: {key}")
        
        k_lower = key.lower()
        if any(b in k_lower for b in blocked_fragments):
            raise GraphVerificationError(f"Prohibited/secret material rejected in graph property key: {key}")

        if not isinstance(val, (str, int, float, bool)):
            raise GraphVerificationError(f"Property value must be str, int, float, or bool. Got {type(val)} for key {key}")
        
        if isinstance(val, str) and len(val) > MAX_STRING_PROPERTY_LENGTH:
            raise GraphVerificationError(f"String property value exceeds MAX_STRING_PROPERTY_LENGTH for key {key}")
            
        clean_props[key] = val
        
    return MappingProxyType(clean_props)


def _serialize_properties(properties: Mapping[str, Any]) -> str:
    """Canonical, deterministic JSON serialization."""
    try:
        # allow_nan=False rejects float('inf') and float('nan')
        return json.dumps(dict(properties), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError as e:
        raise GraphVerificationError(f"Non-canonical property value: {e}")


@dataclass(frozen=True, slots=True)
class GraphProvenance:
    source_system: str
    fact_id: str
    revision: int

    def __post_init__(self) -> None:
        if self.source_system != "sqlite_memory_os_facts":
            raise GraphVerificationError(f"Unsupported source_system: {self.source_system}")
        if not isinstance(self.fact_id, str):
            raise GraphVerificationError("fact_id must be a string")
        if not self.fact_id.strip() or not re.match(r"^\S+$", self.fact_id):
            raise GraphVerificationError("fact_id must not be empty or contain whitespace")
        if type(self.revision) is not int or self.revision < 0:
            raise GraphVerificationError("revision must be a non-negative integer (not bool)")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "source_system": self.source_system,
            "fact_id": self.fact_id,
            "revision": self.revision
        }


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    node_type: str
    properties: MappingProxyType[str, Any]
    provenance: GraphProvenance

    @classmethod
    def create(cls, node_type: str, properties: Mapping[str, Any], provenance: GraphProvenance) -> GraphNode:
        _validate_type_string(node_type, "node_type")
        safe_props = _reject_secrets_and_validate_properties(properties)
        canonical_props = _serialize_properties(safe_props)

        hasher = hashlib.sha256()
        hasher.update(f"v{GRAPH_SCHEMA_VERSION}:node:{node_type}:".encode("utf-8"))
        hasher.update(canonical_props.encode("utf-8"))
        node_id = f"gn_{hasher.hexdigest()}"

        return cls(node_id, node_type, safe_props, provenance)

    def verify_identity(self) -> None:
        expected = self.create(self.node_type, dict(self.properties), self.provenance)
        if self.node_id != expected.node_id:
            raise GraphVerificationError(f"Tampered node identity: {self.node_id} != {expected.node_id}")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "properties": dict(self.properties),
            "provenance": self.provenance.canonical_dict(),
        }


@dataclass(frozen=True, slots=True)
class GraphEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    relation_type: str
    properties: MappingProxyType[str, Any]
    provenance: GraphProvenance

    @classmethod
    def create(cls, source_node_id: str, target_node_id: str, relation_type: str, properties: Mapping[str, Any], provenance: GraphProvenance) -> GraphEdge:
        if source_node_id == target_node_id:
            raise GraphVerificationError("Self-edges are not permitted.")
        if not source_node_id.startswith("gn_") or not target_node_id.startswith("gn_"):
            raise GraphVerificationError("Invalid node references.")
            
        _validate_type_string(relation_type, "relation_type")
        safe_props = _reject_secrets_and_validate_properties(properties)
        canonical_props = _serialize_properties(safe_props)

        hasher = hashlib.sha256()
        hasher.update(f"v{GRAPH_SCHEMA_VERSION}:edge:{source_node_id}:{target_node_id}:{relation_type}:".encode("utf-8"))
        hasher.update(canonical_props.encode("utf-8"))
        edge_id = f"ge_{hasher.hexdigest()}"

        return cls(edge_id, source_node_id, target_node_id, relation_type, safe_props, provenance)

    def verify_identity(self) -> None:
        expected = self.create(self.source_node_id, self.target_node_id, self.relation_type, dict(self.properties), self.provenance)
        if self.edge_id != expected.edge_id:
            raise GraphVerificationError(f"Tampered edge identity: {self.edge_id} != {expected.edge_id}")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "relation_type": self.relation_type,
            "properties": dict(self.properties),
            "provenance": self.provenance.canonical_dict(),
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeFactState:
    fact_id: str
    current_revision: int
    status: str

    def __post_init__(self) -> None:
        if not isinstance(self.fact_id, str) or not self.fact_id.strip() or not re.match(r"^\S+$", self.fact_id):
            raise GraphVerificationError("Invalid fact_id in state")
        if type(self.current_revision) is not int or self.current_revision < 0:
            raise GraphVerificationError("current_revision must be >= 0")
        if self.status not in ("ACTIVE", "DELETED"):
            raise GraphVerificationError("status must be ACTIVE or DELETED")


@dataclass(frozen=True, slots=True)
class AuthoritativeSourceSnapshot:
    facts: tuple[AuthoritativeFactState, ...]
    is_complete: bool


def classify_provenance(prov: GraphProvenance, source: AuthoritativeSourceSnapshot) -> str:
    for fact in source.facts:
        if fact.fact_id == prov.fact_id:
            if fact.status == "DELETED":
                return "DELETED_SOURCE"
            if prov.revision < fact.current_revision:
                return "STALE"
            if prov.revision == fact.current_revision:
                return "CURRENT"
            # If prov.revision > fact.current_revision, it's weird, but typically stale/invalid.
            # Treat as UNKNOWN_SOURCE or just let it fall through
            return "UNKNOWN_SOURCE"
    if source.is_complete:
        return "UNKNOWN_SOURCE"
    # Cannot determine if partial snapshot
    raise GraphVerificationError("Evidence sufficiency is ambiguous (partial snapshot missing fact)")


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    schema_version: int
    snapshot_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    authoritative_source: AuthoritativeSourceSnapshot | None

    @classmethod
    def create(cls, nodes: list[GraphNode], edges: list[GraphEdge], authoritative_source: AuthoritativeSourceSnapshot | None = None) -> GraphSnapshot:
        if len(nodes) > MAX_GRAPH_NODES:
            raise GraphVerificationError("Node count exceeds MAX_GRAPH_NODES")
        if len(edges) > MAX_GRAPH_EDGES:
            raise GraphVerificationError("Edge count exceeds MAX_GRAPH_EDGES")

        node_ids = set()
        node_dicts = []
        for n in nodes:
            n.verify_identity()
            if n.node_id in node_ids:
                raise GraphVerificationError(f"Duplicate node_id rejected: {n.node_id}")
            node_ids.add(n.node_id)
            node_dicts.append(n.canonical_dict())

        edge_ids = set()
        edge_dicts = []
        logical_edges = {}
        for e in edges:
            e.verify_identity()
            if e.edge_id in edge_ids:
                raise GraphVerificationError(f"Duplicate edge_id rejected: {e.edge_id}")
            if e.source_node_id not in node_ids or e.target_node_id not in node_ids:
                raise GraphVerificationError(f"Dangling edge reference in edge {e.edge_id}")
            
            logical_key = (e.source_node_id, e.target_node_id, e.relation_type)
            if logical_key in logical_edges:
                raise GraphVerificationError(f"Conflict detected for logical edge {logical_key}")
            logical_edges[logical_key] = e.edge_id

            edge_ids.add(e.edge_id)
            edge_dicts.append(e.canonical_dict())

        node_dicts.sort(key=lambda x: x["node_id"])
        edge_dicts.sort(key=lambda x: x["edge_id"])
        
        # We don't include authoritative_source in the content hash to keep snapshot identity tied to the derived output.
        canonical_nodes = json.dumps(node_dicts, separators=(",", ":"))
        canonical_edges = json.dumps(edge_dicts, separators=(",", ":"))
        
        hasher = hashlib.sha256()
        hasher.update(f"v{GRAPH_SCHEMA_VERSION}:snapshot:".encode("utf-8"))
        hasher.update(canonical_nodes.encode("utf-8"))
        hasher.update(b":")
        hasher.update(canonical_edges.encode("utf-8"))
        snapshot_id = f"gs_{hasher.hexdigest()}"

        return cls(GRAPH_SCHEMA_VERSION, snapshot_id, tuple(nodes), tuple(edges), authoritative_source)

    def verify_identity(self) -> None:
        expected = self.create(list(self.nodes), list(self.edges), self.authoritative_source)
        if self.snapshot_id != expected.snapshot_id:
            raise GraphVerificationError("Tampered snapshot identity")

    def canonical_dict(self) -> dict[str, Any]:
        d = {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "nodes": [n.canonical_dict() for n in self.nodes],
            "edges": [e.canonical_dict() for e in self.edges],
        }
        if self.authoritative_source:
            d["authoritative_source"] = {
                "facts": [{"fact_id": f.fact_id, "current_revision": f.current_revision, "status": f.status} 
                          for f in self.authoritative_source.facts],
                "is_complete": self.authoritative_source.is_complete
            }
        else:
            d["authoritative_source"] = None
        return d


def serialize_graph_snapshot(snapshot: GraphSnapshot) -> str:
    snapshot.verify_identity()
    return json.dumps(snapshot.canonical_dict(), sort_keys=True, separators=(",", ":"))

def deserialize_graph_snapshot(json_str: str) -> GraphSnapshot:
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError:
        raise GraphVerificationError("Invalid JSON")
    
    if not isinstance(data, dict):
        raise GraphVerificationError("Root must be object")
    if data.get("schema_version") != GRAPH_SCHEMA_VERSION:
        raise GraphVerificationError("Unsupported schema_version")

    def _parse_prov(p: dict) -> GraphProvenance:
        return GraphProvenance(p["source_system"], p["fact_id"], p["revision"])
        
    nodes = []
    for n in data.get("nodes", []):
        nodes.append(GraphNode(n["node_id"], n["node_type"], MappingProxyType(n["properties"]), _parse_prov(n["provenance"])))

    edges = []
    for e in data.get("edges", []):
        edges.append(GraphEdge(e["edge_id"], e["source_node_id"], e["target_node_id"], e["relation_type"], MappingProxyType(e["properties"]), _parse_prov(e["provenance"])))

    auth = None
    if data.get("authoritative_source"):
        ad = data["authoritative_source"]
        facts = tuple(AuthoritativeFactState(f["fact_id"], f["current_revision"], f["status"]) for f in ad["facts"])
        auth = AuthoritativeSourceSnapshot(facts, ad["is_complete"])

    snap = GraphSnapshot(data["schema_version"], data["snapshot_id"], tuple(nodes), tuple(edges), auth)
    snap.verify_identity()
    return snap
