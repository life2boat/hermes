"""Validation and canonical serialization for TaskIntent and TaskLineage schemas v1."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
try:
    from enum import StrEnum
except ImportError:
    from enum import Enum
    class StrEnum(str, Enum):
        pass
from typing import Any, NoReturn, TypeVar

from ai_engineering.contracts import StopBoundary, TaskClass

TASK_INTENT_SCHEMA_VERSION = 1
LINEAGE_SCHEMA_VERSION = 1
MAX_INTENT_BYTES = 512 * 1024

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CRITERION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

class IntentStatus(StrEnum):
    DRAFT = "DRAFT"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    READY = "READY"

class NodeKind(StrEnum):
    INTENT = "INTENT"
    CRITERION = "CRITERION"
    DESIGN = "DESIGN"
    TASK = "TASK"
    EVIDENCE = "EVIDENCE"

class RelationKind(StrEnum):
    DERIVED_FROM = "DERIVED_FROM"
    IMPLEMENTS = "IMPLEMENTS"
    VERIFIES = "VERIFIES"
    SUPERSEDES = "SUPERSEDES"


class TaskIntentValidationError(ValueError):
    """Fail-closed validation error exposing only a stable code."""
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LineageValidationError(ValueError):
    """Fail-closed validation error exposing only a stable code."""
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_EnumT = TypeVar("_EnumT", IntentStatus, TaskClass, StopBoundary, NodeKind, RelationKind)


def _fail_intent(code: str) -> NoReturn:
    raise TaskIntentValidationError(code)

def _fail_lineage(code: str) -> NoReturn:
    raise LineageValidationError(code)


def _mapping(value: object, fail_func) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        fail_func("REQUIRED_FIELD_MISSING")
    return value


def _exact_fields(value: object, expected: frozenset[str], fail_func) -> Mapping[str, object]:
    payload = _mapping(value, fail_func)
    keys = frozenset(payload)
    if expected - keys:
        fail_func("REQUIRED_FIELD_MISSING")
    if keys - expected:
        fail_func("UNEXPECTED_FIELD")
    return payload


def _enum(value: object, enum_type: type[_EnumT], code: str, fail_func) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        fail_func(code)
    try:
        return enum_type(value)
    except ValueError:
        fail_func(code)


def _identifier(value: object, code: str = "VALUE_INVALID", fail_func=None) -> str:
    fail = fail_func or _fail_intent
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        fail(code)
    return value

def _string(value: object, fail_func) -> str:
    if not isinstance(value, str):
        fail_func("VALUE_INVALID")
    return value


def _boolean(value: object, fail_func) -> bool:
    if not isinstance(value, bool):
        fail_func("VALUE_INVALID")
    return value


def _items(value: object, fail_func) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        fail_func("VALUE_INVALID")
    return value


@dataclass(frozen=True, slots=True)
class IntentUnknown:
    unknown_id: str
    description: str
    blocking: bool

@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    criterion_id: str
    statement: str

@dataclass(frozen=True, slots=True)
class TaskIntent:
    schema_version: int
    task_id: str
    intent_revision: int
    status: IntentStatus
    task_class: TaskClass
    desired_outcome: str
    source_repository: str
    source_main_ref: str
    source_base_sha: str
    constraints: tuple[str, ...]
    allowed_mutations: tuple[str, ...]
    forbidden_mutations: tuple[str, ...]
    stop_boundary: StopBoundary
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    unknowns: tuple[IntentUnknown, ...]
    applicable_invariants: tuple[str, ...]
    required_gates: tuple[str, ...]
    parent_intent_digest: str | None = None

_UNKNOWN_FIELDS = frozenset({"unknown_id", "description", "blocking"})
_CRITERION_FIELDS = frozenset({"criterion_id", "statement"})

_INTENT_FIELDS = frozenset({
    "schema_version",
    "task_id",
    "intent_revision",
    "status",
    "task_class",
    "desired_outcome",
    "source_repository",
    "source_main_ref",
    "source_base_sha",
    "constraints",
    "allowed_mutations",
    "forbidden_mutations",
    "stop_boundary",
    "acceptance_criteria",
    "unknowns",
    "applicable_invariants",
    "required_gates",
    "parent_intent_digest",
})


def _validate_path(path: str, fail_func) -> None:
    if not isinstance(path, str) or not path:
        fail_func("VALUE_INVALID")
    if path.startswith("/") or path.startswith("\\"):
        fail_func("ABSOLUTE_PATH_FORBIDDEN")
    if ":" in path:  # Windows drive letter C:
        fail_func("DRIVE_PATH_FORBIDDEN")
    if path.startswith(r"\\") or path.startswith("//"):
        fail_func("UNC_PATH_FORBIDDEN")
    parts = path.replace("\\", "/").split("/")
    for part in parts:
        if part == "..":
            fail_func("PATH_TRAVERSAL_FORBIDDEN")


def _intent_from_mapping(value: Mapping[str, object]) -> TaskIntent:
    raw_version = value.get("schema_version")
    if raw_version != TASK_INTENT_SCHEMA_VERSION or isinstance(raw_version, bool):
        _fail_intent("SCHEMA_VERSION_UNSUPPORTED")
    
    payload = _exact_fields(value, _INTENT_FIELDS, _fail_intent)
    
    task_id = _identifier(payload["task_id"], fail_func=_fail_intent)
    if not isinstance(payload["intent_revision"], int) or payload["intent_revision"] < 1:
        _fail_intent("VALUE_INVALID")
    
    status = _enum(payload["status"], IntentStatus, "STATUS_INVALID", _fail_intent)
    
    if not isinstance(payload["desired_outcome"], str) or not payload["desired_outcome"].strip():
        _fail_intent("DESIRED_OUTCOME_EMPTY")
        
    base_sha = payload["source_base_sha"]
    if not isinstance(base_sha, str) or _SHA_RE.fullmatch(base_sha) is None:
        _fail_intent("VALUE_INVALID")

    constraints = tuple(_string(x, _fail_intent) for x in _items(payload["constraints"], _fail_intent))
    
    allowed = tuple(_string(x, _fail_intent) for x in _items(payload["allowed_mutations"], _fail_intent))
    for p in allowed:
        _validate_path(p, _fail_intent)
        
    forbidden = tuple(_string(x, _fail_intent) for x in _items(payload["forbidden_mutations"], _fail_intent))
    for p in forbidden:
        _validate_path(p, _fail_intent)

    criteria: list[AcceptanceCriterion] = []
    crit_ids = set()
    for item in _items(payload["acceptance_criteria"], _fail_intent):
        crit_payload = _exact_fields(item, _CRITERION_FIELDS, _fail_intent)
        cid = _string(crit_payload["criterion_id"], _fail_intent)
        if _CRITERION_ID_RE.fullmatch(cid) is None:
            _fail_intent("VALUE_INVALID")
        if cid in crit_ids:
            _fail_intent("DUPLICATE_ACCEPTANCE_CRITERION_ID")
        crit_ids.add(cid)
        criteria.append(AcceptanceCriterion(
            criterion_id=cid,
            statement=_string(crit_payload["statement"], _fail_intent)
        ))

    unknowns: list[IntentUnknown] = []
    has_blocking = False
    unk_ids = set()
    for item in _items(payload["unknowns"], _fail_intent):
        unk_payload = _exact_fields(item, _UNKNOWN_FIELDS, _fail_intent)
        uid = _identifier(unk_payload["unknown_id"], fail_func=_fail_intent)
        if uid in unk_ids:
            _fail_intent("VALUE_INVALID")
        unk_ids.add(uid)
        blocking = _boolean(unk_payload["blocking"], _fail_intent)
        if blocking:
            has_blocking = True
        unknowns.append(IntentUnknown(
            unknown_id=uid,
            description=_string(unk_payload["description"], _fail_intent),
            blocking=blocking
        ))

    if status == IntentStatus.READY and has_blocking:
        _fail_intent("READY_WITH_BLOCKING_UNKNOWN")

    applicable_invariants = tuple(_string(x, _fail_intent) for x in _items(payload["applicable_invariants"], _fail_intent))
    required_gates = tuple(_string(x, _fail_intent) for x in _items(payload["required_gates"], _fail_intent))
    
    parent_digest = payload["parent_intent_digest"]
    if parent_digest is not None:
        if not isinstance(parent_digest, str) or _DIGEST_RE.fullmatch(parent_digest) is None:
            _fail_intent("VALUE_INVALID")

    return TaskIntent(
        schema_version=TASK_INTENT_SCHEMA_VERSION,
        task_id=task_id,
        intent_revision=payload["intent_revision"], # type: ignore
        status=status,
        task_class=_enum(payload["task_class"], TaskClass, "VALUE_INVALID", _fail_intent),
        desired_outcome=payload["desired_outcome"], # type: ignore
        source_repository=_identifier(payload["source_repository"], fail_func=_fail_intent),
        source_main_ref=_string(payload["source_main_ref"], _fail_intent),
        source_base_sha=base_sha,
        constraints=constraints,
        allowed_mutations=allowed,
        forbidden_mutations=forbidden,
        stop_boundary=_enum(payload["stop_boundary"], StopBoundary, "VALUE_INVALID", _fail_intent),
        acceptance_criteria=tuple(criteria),
        unknowns=tuple(unknowns),
        applicable_invariants=applicable_invariants,
        required_gates=required_gates,
        parent_intent_digest=parent_digest,
    )


def _intent_to_dict(intent: TaskIntent) -> dict[str, object]:
    return {
        "schema_version": intent.schema_version,
        "task_id": intent.task_id,
        "intent_revision": intent.intent_revision,
        "status": intent.status.value,
        "task_class": intent.task_class.value,
        "desired_outcome": intent.desired_outcome,
        "source_repository": intent.source_repository,
        "source_main_ref": intent.source_main_ref,
        "source_base_sha": intent.source_base_sha,
        "constraints": list(intent.constraints),
        "allowed_mutations": list(intent.allowed_mutations),
        "forbidden_mutations": list(intent.forbidden_mutations),
        "stop_boundary": intent.stop_boundary.value,
        "acceptance_criteria": [
            {
                "criterion_id": c.criterion_id,
                "statement": c.statement,
            }
            for c in intent.acceptance_criteria
        ],
        "unknowns": [
            {
                "unknown_id": u.unknown_id,
                "description": u.description,
                "blocking": u.blocking,
            }
            for u in intent.unknowns
        ],
        "applicable_invariants": list(intent.applicable_invariants),
        "required_gates": list(intent.required_gates),
        "parent_intent_digest": intent.parent_intent_digest,
    }


def validate_intent(value: TaskIntent | Mapping[str, object]) -> TaskIntent:
    if isinstance(value, TaskIntent):
        return _intent_from_mapping(_intent_to_dict(value))
    return _intent_from_mapping(_mapping(value, _fail_intent))


def normalize_intent(value: TaskIntent | Mapping[str, object]) -> dict[str, object]:
    return _intent_to_dict(validate_intent(value))


def serialize_intent(value: TaskIntent | Mapping[str, object]) -> str:
    try:
        return json.dumps(
            normalize_intent(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise TaskIntentValidationError("VALUE_INVALID") from exc


class _DuplicateJsonKey(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError


def deserialize_intent(value: str | bytes) -> TaskIntent:
    if isinstance(value, bytes):
        raw = value
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TaskIntentValidationError("JSON_INVALID") from exc
    elif isinstance(value, str):
        text = value
        raw = value.encode("utf-8")
    else:
        _fail_intent("JSON_INVALID")
    
    if not raw or len(raw) > MAX_INTENT_BYTES or b"\x00" in raw:
        _fail_intent("JSON_INVALID")
        
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, _DuplicateJsonKey, ValueError, RecursionError) as exc:
        raise TaskIntentValidationError("JSON_INVALID") from exc
        
    return _intent_from_mapping(_mapping(payload, _fail_intent))


def intent_digest(value: TaskIntent | Mapping[str, object]) -> str:
    return hashlib.sha256(serialize_intent(value).encode("utf-8")).hexdigest()


def validate_intent_revision(parent: TaskIntent, current: TaskIntent) -> None:
    if parent.task_id != current.task_id:
        _fail_intent("BROKEN_PARENT_IDENTITY")
    if current.intent_revision != parent.intent_revision + 1:
        _fail_intent("BROKEN_PARENT_IDENTITY")
    if current.parent_intent_digest != intent_digest(parent):
        _fail_intent("BROKEN_PARENT_IDENTITY")
    if current.parent_intent_digest == intent_digest(current):
        _fail_intent("SELF_SUPERSESSION")


@dataclass(frozen=True, slots=True)
class LineageNode:
    node_id: str
    kind: NodeKind


@dataclass(frozen=True, slots=True)
class LineageEdge:
    source_id: str
    target_id: str
    relation: RelationKind


@dataclass(frozen=True, slots=True)
class TaskLineage:
    schema_version: int
    nodes: tuple[LineageNode, ...]
    edges: tuple[LineageEdge, ...]

_NODE_FIELDS = frozenset({"node_id", "kind"})
_EDGE_FIELDS = frozenset({"source_id", "target_id", "relation"})
_LINEAGE_FIELDS = frozenset({"schema_version", "nodes", "edges"})

_VALID_RELATIONS = frozenset({
    (NodeKind.DESIGN, RelationKind.DERIVED_FROM, NodeKind.INTENT),
    (NodeKind.TASK, RelationKind.IMPLEMENTS, NodeKind.CRITERION),
    (NodeKind.EVIDENCE, RelationKind.VERIFIES, NodeKind.TASK),
    (NodeKind.EVIDENCE, RelationKind.VERIFIES, NodeKind.CRITERION),
    (NodeKind.INTENT, RelationKind.SUPERSEDES, NodeKind.INTENT),
})

def _lineage_from_mapping(value: Mapping[str, object]) -> TaskLineage:
    raw_version = value.get("schema_version")
    if raw_version != LINEAGE_SCHEMA_VERSION or isinstance(raw_version, bool):
        _fail_lineage("SCHEMA_VERSION_UNSUPPORTED")
        
    payload = _exact_fields(value, _LINEAGE_FIELDS, _fail_lineage)
    
    nodes: list[LineageNode] = []
    node_kinds: dict[str, NodeKind] = {}
    
    for item in _items(payload["nodes"], _fail_lineage):
        n_payload = _exact_fields(item, _NODE_FIELDS, _fail_lineage)
        nid = _string(n_payload["node_id"], _fail_lineage)
        if nid in node_kinds:
            _fail_lineage("DUPLICATE_NODE_ID")
        kind = _enum(n_payload["kind"], NodeKind, "INVALID_NODE_KIND", _fail_lineage)
        node_kinds[nid] = kind
        nodes.append(LineageNode(node_id=nid, kind=kind))

    edges: list[LineageEdge] = []
    # Build graph to detect cycles.
    graph: dict[str, list[str]] = {nid: [] for nid in node_kinds}
    # Keep track of in-degrees for cycle detection.
    in_degree: dict[str, int] = {nid: 0 for nid in node_kinds}
    
    has_targets: set[str] = set()
    has_sources: set[str] = set()
    
    for item in _items(payload["edges"], _fail_lineage):
        e_payload = _exact_fields(item, _EDGE_FIELDS, _fail_lineage)
        sid = _string(e_payload["source_id"], _fail_lineage)
        tid = _string(e_payload["target_id"], _fail_lineage)
        relation = _enum(e_payload["relation"], RelationKind, "INVALID_RELATION_KIND", _fail_lineage)
        
        if sid not in node_kinds or tid not in node_kinds:
            _fail_lineage("DANGLING_EDGE")
            
        skind = node_kinds[sid]
        tkind = node_kinds[tid]
        
        if (skind, relation, tkind) not in _VALID_RELATIONS:
            _fail_lineage("INVALID_RELATION_DIRECTION")
            
        edges.append(LineageEdge(source_id=sid, target_id=tid, relation=relation))
        
        # Supercedes relation shouldn't create structural cycles if revisions are linear,
        # but let's check general directed cycles.
        graph[sid].append(tid)
        in_degree[tid] += 1
        has_sources.add(sid)
        has_targets.add(tid)

    # Detect cycles (Topological sort)
    visited_count = 0
    queue = [n for n in node_kinds if in_degree[n] == 0]
    while queue:
        curr = queue.pop(0)
        visited_count += 1
        for neighbor in graph[curr]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
                
    if visited_count != len(node_kinds):
        _fail_lineage("UNINTENDED_CYCLE")

    return TaskLineage(
        schema_version=LINEAGE_SCHEMA_VERSION,
        nodes=tuple(nodes),
        edges=tuple(edges),
    )


def _lineage_to_dict(lineage: TaskLineage) -> dict[str, object]:
    return {
        "schema_version": lineage.schema_version,
        "nodes": [
            {
                "node_id": n.node_id,
                "kind": n.kind.value,
            } for n in lineage.nodes
        ],
        "edges": [
            {
                "source_id": e.source_id,
                "target_id": e.target_id,
                "relation": e.relation.value,
            } for e in lineage.edges
        ],
    }

def validate_lineage(value: TaskLineage | Mapping[str, object]) -> TaskLineage:
    if isinstance(value, TaskLineage):
        return _lineage_from_mapping(_lineage_to_dict(value))
    return _lineage_from_mapping(_mapping(value, _fail_lineage))

def normalize_lineage(value: TaskLineage | Mapping[str, object]) -> dict[str, object]:
    return _lineage_to_dict(validate_lineage(value))

def serialize_lineage(value: TaskLineage | Mapping[str, object]) -> str:
    try:
        return json.dumps(
            normalize_lineage(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise LineageValidationError("VALUE_INVALID") from exc
