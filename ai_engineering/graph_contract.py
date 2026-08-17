"""
Deterministic Graph Contract Foundation (v3)

This module provides the architectural and executable foundation for
the derived graph memory layer.

AUTHORITY BOUNDARY:
- SQLite `memory_os_facts` remains AUTHORITATIVE.
- Graph relations produced by this module are DERIVED_REBUILDABLE.
- No LLM-proposed relation is trusted until bound to an authoritative
  source fact via `GraphProvenance`.
- Graph state can be safely deleted and rebuilt from SQLite facts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

GRAPH_SCHEMA_VERSION = 1


class GraphVerificationError(ValueError):
    """Raised when graph assertions fail determinism, validation, or tamper checks."""
    pass


def _reject_secrets(properties: Mapping[str, Any]) -> None:
    """Fail-closed on suspected secret/credential material."""
    blocked_fragments = {"secret", "password", "token", "api_key", "credential"}
    for key in properties:
        k = key.lower()
        if any(b in k for b in blocked_fragments):
            raise GraphVerificationError(f"Secret-like material rejected in graph property: {key}")


def _serialize_properties(properties: Mapping[str, Any]) -> str:
    """Canonical, deterministic JSON serialization."""
    _reject_secrets(properties)
    try:
        # allow_nan=False rejects float('inf') and float('nan') which are non-canonical in JSON
        return json.dumps(properties, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError as e:
        raise GraphVerificationError(f"Non-canonical property value: {e}")


@dataclass(frozen=True, slots=True)
class GraphProvenance:
    """Explicit source binding to authoritative memory."""
    source_system: str  # e.g., "sqlite_memory_os_facts"
    fact_id: str | int  # The canonical fact ID
    revision: int       # The canonical fact revision

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "source_system": self.source_system,
            "fact_id": str(self.fact_id),
            "revision": self.revision
        }


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    node_type: str
    properties: Mapping[str, Any]
    provenance: GraphProvenance

    @classmethod
    def create(cls, node_type: str, properties: Mapping[str, Any], provenance: GraphProvenance) -> GraphNode:
        canonical_props = _serialize_properties(properties)
        canonical_prov = json.dumps(provenance.canonical_dict(), sort_keys=True, separators=(",", ":"))

        hasher = hashlib.sha256()
        hasher.update(f"v{GRAPH_SCHEMA_VERSION}:node:{node_type}:".encode("utf-8"))
        hasher.update(canonical_props.encode("utf-8"))
        hasher.update(b":")
        hasher.update(canonical_prov.encode("utf-8"))
        node_id = f"gn_{hasher.hexdigest()[:32]}"

        return cls(
            node_id=node_id,
            node_type=node_type,
            properties=properties,
            provenance=provenance
        )

    def verify_identity(self) -> None:
        expected = self.create(self.node_type, self.properties, self.provenance)
        if self.node_id != expected.node_id:
            raise GraphVerificationError(f"Tampered or non-deterministic node identity: {self.node_id} != {expected.node_id}")

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
    properties: Mapping[str, Any]
    provenance: GraphProvenance

    @classmethod
    def create(cls, source_node_id: str, target_node_id: str, relation_type: str, properties: Mapping[str, Any], provenance: GraphProvenance) -> GraphEdge:
        if source_node_id == target_node_id:
            raise GraphVerificationError("Self-edges are not permitted in the deterministic graph contract.")

        canonical_props = _serialize_properties(properties)
        canonical_prov = json.dumps(provenance.canonical_dict(), sort_keys=True, separators=(",", ":"))

        hasher = hashlib.sha256()
        hasher.update(f"v{GRAPH_SCHEMA_VERSION}:edge:{source_node_id}:{target_node_id}:{relation_type}:".encode("utf-8"))
        hasher.update(canonical_props.encode("utf-8"))
        hasher.update(b":")
        hasher.update(canonical_prov.encode("utf-8"))
        edge_id = f"ge_{hasher.hexdigest()[:32]}"

        return cls(
            edge_id=edge_id,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            relation_type=relation_type,
            properties=properties,
            provenance=provenance
        )

    def verify_identity(self) -> None:
        if self.source_node_id == self.target_node_id:
            raise GraphVerificationError("Self-edges are not permitted.")

        expected = self.create(self.source_node_id, self.target_node_id, self.relation_type, self.properties, self.provenance)
        if self.edge_id != expected.edge_id:
            raise GraphVerificationError(f"Tampered or non-deterministic edge identity: {self.edge_id} != {expected.edge_id}")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "relation_type": self.relation_type,
            "properties": dict(self.properties),
            "provenance": self.provenance.canonical_dict(),
        }
