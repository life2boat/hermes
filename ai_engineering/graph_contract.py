from __future__ import annotations
import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

GRAPH_SCHEMA_VERSION = 1

MAX_GRAPH_NODES = 1000
MAX_GRAPH_EDGES = 5000
MAX_PROPERTIES_PER_ENTITY = 100
MAX_PROPERTY_KEY_LENGTH = 100
MAX_STRING_PROPERTY_LENGTH = 4096


class GraphVerificationError(ValueError):
    pass


def _validate_type_string(val: Any, field_name: str) -> None:
    if type(val) is not str or not re.match(r"^[a-z0-9_]+:[a-z0-9_]+$", val):
        raise GraphVerificationError(f"Invalid {field_name}: '{val}'")


def _validate_node_id(val: Any) -> None:
    if type(val) is not str or not re.match(r"^gn_[a-f0-9]{64}$", val):
        raise GraphVerificationError(f"Invalid node_id: '{val}'")


def _validate_edge_id(val: Any) -> None:
    if type(val) is not str or not re.match(r"^ge_[a-f0-9]{64}$", val):
        raise GraphVerificationError(f"Invalid edge_id: '{val}'")


def _validate_snapshot_id(val: Any) -> None:
    if type(val) is not str or not re.match(r"^gs_[a-f0-9]{64}$", val):
        raise GraphVerificationError(f"Invalid snapshot_id: '{val}'")


def _reject_secrets_and_validate_properties(
    properties: Any,
) -> MappingProxyType[str, Any]:
    if not isinstance(properties, Mapping):
        raise GraphVerificationError("properties must be a Mapping")
    if len(properties) > MAX_PROPERTIES_PER_ENTITY:
        raise GraphVerificationError(f"properties exceed MAX_PROPERTIES_PER_ENTITY")

    blocked_fragments = {
        "raw_prompt",
        "compiled_prompt",
        "chain_of_thought",
        "cot",
        "credential",
        "password",
        "api_key",
        "secret",
        "access_token",
        "refresh_token",
        "provider_raw_response",
    }

    clean_props = {}
    for key, val in properties.items():
        if type(key) is not str:
            raise GraphVerificationError("Property keys must be strings")
        if len(key) > MAX_PROPERTY_KEY_LENGTH:
            raise GraphVerificationError(
                f"Property key length exceeds MAX_PROPERTY_KEY_LENGTH"
            )

        k_lower = key.lower()
        if any(b in k_lower for b in blocked_fragments):
            raise GraphVerificationError(f"Prohibited/secret material rejected")

        if type(val) not in (str, int, float, bool):
            raise GraphVerificationError(
                f"Property value must be str, int, float, or bool."
            )

        if type(val) is str and len(val) > MAX_STRING_PROPERTY_LENGTH:
            raise GraphVerificationError(
                f"String property value exceeds MAX_STRING_PROPERTY_LENGTH"
            )

        clean_props[key] = val

    return MappingProxyType(clean_props)


def _serialize_properties(properties: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(properties), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except ValueError as e:
        raise GraphVerificationError(f"Non-canonical property value: {e}")


@dataclass(frozen=True, slots=True)
class GraphProvenance:
    source_system: str
    fact_id: str
    revision: int

    def __post_init__(self) -> None:
        if self.source_system != "sqlite_memory_os_facts":
            raise GraphVerificationError(
                f"Unsupported source_system: {self.source_system}"
            )
        if type(self.fact_id) is not str:
            raise GraphVerificationError("fact_id must be a string")
        if not self.fact_id.strip() or not re.match(r"^\S+$", self.fact_id):
            raise GraphVerificationError(
                "fact_id must not be empty or contain whitespace"
            )
        if type(self.revision) is not int or self.revision < 0:
            raise GraphVerificationError("revision must be a non-negative integer")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "source_system": self.source_system,
            "fact_id": self.fact_id,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class GraphNode:
    node_id: str
    node_type: str
    properties: MappingProxyType[str, Any]
    provenance: GraphProvenance

    def __post_init__(self) -> None:
        _validate_node_id(self.node_id)
        _validate_type_string(self.node_type, "node_type")
        if type(self.properties) is not MappingProxyType:
            raise GraphVerificationError(
                "properties must be MappingProxyType at instantiation"
            )
        if type(self.provenance) is not GraphProvenance:
            raise GraphVerificationError("provenance must be GraphProvenance")

    @classmethod
    def create(
        cls, node_type: str, properties: Mapping[str, Any], provenance: GraphProvenance
    ) -> GraphNode:
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
            raise GraphVerificationError(
                f"Tampered node identity: {self.node_id} != {expected.node_id}"
            )

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

    def __post_init__(self) -> None:
        _validate_edge_id(self.edge_id)
        _validate_node_id(self.source_node_id)
        _validate_node_id(self.target_node_id)
        _validate_type_string(self.relation_type, "relation_type")
        if type(self.properties) is not MappingProxyType:
            raise GraphVerificationError("properties must be MappingProxyType")
        if type(self.provenance) is not GraphProvenance:
            raise GraphVerificationError("provenance must be GraphProvenance")

    @classmethod
    def create(
        cls,
        source_node_id: str,
        target_node_id: str,
        relation_type: str,
        properties: Mapping[str, Any],
        provenance: GraphProvenance,
    ) -> GraphEdge:
        if source_node_id == target_node_id:
            raise GraphVerificationError("Self-edges are not permitted.")
        _validate_node_id(source_node_id)
        _validate_node_id(target_node_id)
        _validate_type_string(relation_type, "relation_type")
        safe_props = _reject_secrets_and_validate_properties(properties)
        canonical_props = _serialize_properties(safe_props)

        hasher = hashlib.sha256()
        hasher.update(
            f"v{GRAPH_SCHEMA_VERSION}:edge:{source_node_id}:{target_node_id}:{relation_type}:".encode(
                "utf-8"
            )
        )
        hasher.update(canonical_props.encode("utf-8"))
        edge_id = f"ge_{hasher.hexdigest()}"

        return cls(
            edge_id,
            source_node_id,
            target_node_id,
            relation_type,
            safe_props,
            provenance,
        )

    def verify_identity(self) -> None:
        expected = self.create(
            self.source_node_id,
            self.target_node_id,
            self.relation_type,
            dict(self.properties),
            self.provenance,
        )
        if self.edge_id != expected.edge_id:
            raise GraphVerificationError(
                f"Tampered edge identity: {self.edge_id} != {expected.edge_id}"
            )

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
        if (
            type(self.fact_id) is not str
            or not self.fact_id.strip()
            or not re.match(r"^\S+$", self.fact_id)
        ):
            raise GraphVerificationError("Invalid fact_id in state")
        if type(self.current_revision) is not int or self.current_revision < 0:
            raise GraphVerificationError("current_revision must be >= 0")
        if self.status not in ("ACTIVE", "DELETED"):
            raise GraphVerificationError("status must be ACTIVE or DELETED")


@dataclass(frozen=True, slots=True)
class AuthoritativeSourceSnapshot:
    facts: tuple[AuthoritativeFactState, ...]
    is_complete: bool

    def __post_init__(self) -> None:
        if type(self.facts) is not tuple:
            raise GraphVerificationError("facts must be tuple")
        if type(self.is_complete) is not bool:
            raise GraphVerificationError("is_complete must be bool")
        fact_ids = set()
        for f in self.facts:
            if type(f) is not AuthoritativeFactState:
                raise GraphVerificationError("fact must be AuthoritativeFactState")
            if f.fact_id in fact_ids:
                raise GraphVerificationError(f"Duplicate fact_id {f.fact_id}")
            fact_ids.add(f.fact_id)

        sorted_facts = tuple(
            sorted(self.facts, key=lambda f: (f.fact_id, f.current_revision, f.status))
        )
        object.__setattr__(self, "facts", sorted_facts)


def classify_provenance(
    prov: GraphProvenance, source: AuthoritativeSourceSnapshot
) -> str:
    for fact in source.facts:
        if fact.fact_id == prov.fact_id:
            if fact.status == "DELETED":
                return "DELETED_SOURCE"
            if prov.revision < fact.current_revision:
                return "STALE"
            if prov.revision == fact.current_revision:
                return "CURRENT"
            if prov.revision > fact.current_revision:
                raise GraphVerificationError(
                    "Provenance revision cannot be greater than authoritative current_revision"
                )
    if source.is_complete:
        return "UNKNOWN_SOURCE"
    raise GraphVerificationError(
        "Evidence sufficiency is ambiguous (partial snapshot missing fact)"
    )


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    schema_version: int
    snapshot_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    authoritative_source: AuthoritativeSourceSnapshot | None

    def __post_init__(self) -> None:
        if self.schema_version != GRAPH_SCHEMA_VERSION:
            raise GraphVerificationError("Unsupported schema_version")
        _validate_snapshot_id(self.snapshot_id)
        if type(self.nodes) is not tuple or type(self.edges) is not tuple:
            raise GraphVerificationError("nodes and edges must be tuples")

    @classmethod
    def create(
        cls,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        authoritative_source: AuthoritativeSourceSnapshot | None = None,
    ) -> GraphSnapshot:
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
                raise GraphVerificationError(
                    f"Dangling edge reference in edge {e.edge_id}"
                )

            logical_key = (e.source_node_id, e.target_node_id, e.relation_type)
            if logical_key in logical_edges:
                raise GraphVerificationError(
                    f"Conflict detected for logical edge {logical_key}"
                )
            logical_edges[logical_key] = e.edge_id

            edge_ids.add(e.edge_id)
            edge_dicts.append(e.canonical_dict())

        node_dicts.sort(key=lambda x: x["node_id"])
        edge_dicts.sort(key=lambda x: x["edge_id"])

        canonical_nodes = json.dumps(node_dicts, sort_keys=True, separators=(",", ":"))
        canonical_edges = json.dumps(edge_dicts, sort_keys=True, separators=(",", ":"))

        hasher = hashlib.sha256()
        hasher.update(f"v{GRAPH_SCHEMA_VERSION}:snapshot:".encode("utf-8"))
        hasher.update(canonical_nodes.encode("utf-8"))
        hasher.update(b":")
        hasher.update(canonical_edges.encode("utf-8"))
        if authoritative_source:
            hasher.update(b":auth:")
            auth_dict = {
                "facts": [
                    {
                        "fact_id": f.fact_id,
                        "current_revision": f.current_revision,
                        "status": f.status,
                    }
                    for f in authoritative_source.facts
                ],
                "is_complete": authoritative_source.is_complete,
            }
            canonical_auth = json.dumps(
                auth_dict, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            hasher.update(canonical_auth.encode("utf-8"))
        snapshot_id = f"gs_{hasher.hexdigest()}"

        sorted_nodes = tuple(sorted(nodes, key=lambda n: n.node_id))
        sorted_edges = tuple(sorted(edges, key=lambda e: e.edge_id))
        return cls(
            GRAPH_SCHEMA_VERSION,
            snapshot_id,
            sorted_nodes,
            sorted_edges,
            authoritative_source,
        )

    def verify_identity(self) -> None:
        expected = self.create(
            list(self.nodes), list(self.edges), self.authoritative_source
        )
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
                "facts": [
                    {
                        "fact_id": f.fact_id,
                        "current_revision": f.current_revision,
                        "status": f.status,
                    }
                    for f in self.authoritative_source.facts
                ],
                "is_complete": self.authoritative_source.is_complete,
            }
        return d


def serialize_graph_snapshot(snapshot: GraphSnapshot) -> str:
    snapshot.verify_identity()
    return json.dumps(snapshot.canonical_dict(), sort_keys=True, separators=(",", ":"))


def deserialize_graph_snapshot(json_str: str) -> GraphSnapshot:
    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        d = {}
        for k, v in pairs:
            if k in d:
                raise GraphVerificationError(f"Duplicate JSON key rejected: {k}")
            d[k] = v
        return d

    try:
        data = json.loads(json_str, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError:
        raise GraphVerificationError("Invalid JSON")

    if type(data) is not dict:
        raise GraphVerificationError("Root must be object")

    allowed_top_keys = {"schema_version", "snapshot_id", "nodes", "edges"}
    if "authoritative_source" in data:
        allowed_top_keys.add("authoritative_source")

    if not set(data.keys()).issubset(allowed_top_keys):
        raise GraphVerificationError(
            f"Unknown top-level fields: {set(data.keys()) - allowed_top_keys}"
        )

    for k in ("schema_version", "snapshot_id", "nodes", "edges"):
        if k not in data:
            raise GraphVerificationError(f"Missing required top-level field: {k}")

    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != GRAPH_SCHEMA_VERSION
    ):
        raise GraphVerificationError("Unsupported schema_version")

    def _parse_prov(p: Any) -> GraphProvenance:
        if type(p) is not dict:
            raise GraphVerificationError("provenance must be object")
        allowed = {"source_system", "fact_id", "revision"}
        if not set(p.keys()).issubset(allowed):
            raise GraphVerificationError("Unknown provenance fields")
        for k in allowed:
            if k not in p:
                raise GraphVerificationError(f"Missing provenance field: {k}")
        return GraphProvenance(p["source_system"], p["fact_id"], p["revision"])

    if type(data["nodes"]) is not list:
        raise GraphVerificationError("nodes must be array")
    if len(data["nodes"]) > MAX_GRAPH_NODES:
        raise GraphVerificationError("Node count exceeds MAX_GRAPH_NODES")

    nodes = []
    for n in data["nodes"]:
        if type(n) is not dict:
            raise GraphVerificationError("node must be object")
        allowed_n = {"node_id", "node_type", "properties", "provenance"}
        if not set(n.keys()).issubset(allowed_n):
            raise GraphVerificationError("Unknown node fields")
        for k in allowed_n:
            if k not in n:
                raise GraphVerificationError(f"Missing node field: {k}")

        nodes.append(
            GraphNode(
                n["node_id"],
                n["node_type"],
                _reject_secrets_and_validate_properties(n["properties"]),
                _parse_prov(n["provenance"]),
            )
        )

    if type(data["edges"]) is not list:
        raise GraphVerificationError("edges must be array")
    if len(data["edges"]) > MAX_GRAPH_EDGES:
        raise GraphVerificationError("Edge count exceeds MAX_GRAPH_EDGES")

    edges = []
    for e in data["edges"]:
        if type(e) is not dict:
            raise GraphVerificationError("edge must be object")
        allowed_e = {
            "edge_id",
            "source_node_id",
            "target_node_id",
            "relation_type",
            "properties",
            "provenance",
        }
        if not set(e.keys()).issubset(allowed_e):
            raise GraphVerificationError("Unknown edge fields")
        for k in allowed_e:
            if k not in e:
                raise GraphVerificationError(f"Missing edge field: {k}")

        edges.append(
            GraphEdge(
                e["edge_id"],
                e["source_node_id"],
                e["target_node_id"],
                e["relation_type"],
                _reject_secrets_and_validate_properties(e["properties"]),
                _parse_prov(e["provenance"]),
            )
        )

    auth = None
    if "authoritative_source" in data and data["authoritative_source"] is not None:
        ad = data["authoritative_source"]
        if type(ad) is not dict:
            raise GraphVerificationError("authoritative_source must be object")

        allowed_a = {"facts", "is_complete"}
        if not set(ad.keys()).issubset(allowed_a):
            raise GraphVerificationError("Unknown authoritative_source fields")
        for k in allowed_a:
            if k not in ad:
                raise GraphVerificationError(f"Missing authoritative_source field: {k}")

        if type(ad["facts"]) is not list:
            raise GraphVerificationError("facts must be array")

        facts_list = []
        for f in ad["facts"]:
            if type(f) is not dict:
                raise GraphVerificationError("fact must be object")
            allowed_f = {"fact_id", "current_revision", "status"}
            if not set(f.keys()).issubset(allowed_f):
                raise GraphVerificationError("Unknown fact fields")
            for k in allowed_f:
                if k not in f:
                    raise GraphVerificationError(f"Missing fact field: {k}")
            facts_list.append(
                AuthoritativeFactState(f["fact_id"], f["current_revision"], f["status"])
            )

        auth = AuthoritativeSourceSnapshot(tuple(facts_list), ad["is_complete"])

    snap = GraphSnapshot(
        data["schema_version"], data["snapshot_id"], tuple(nodes), tuple(edges), auth
    )
    # The __post_init__ will validate snap properties. But to check duplicates and identities we run verify_identity:
    snap.verify_identity()

    # We must also enforce that no dangling edges exist since GraphSnapshot.__post_init__ doesn't do deep edge logic.
    # verify_identity calls create() which checks duplicate IDs and dangling edges, so we are safe!

    return snap
