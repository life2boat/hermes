import pytest
from ai_engineering.task_intent import (
    TaskLineage,
    LineageNode,
    LineageEdge,
    NodeKind,
    RelationKind,
    LineageValidationError,
    validate_lineage,
)

def test_valid_intent_to_design_to_task_to_evidence():
    payload = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "INTENT-1", "kind": "INTENT"},
            {"node_id": "CRIT-1", "kind": "CRITERION"},
            {"node_id": "DESIGN-1", "kind": "DESIGN"},
            {"node_id": "TASK-1", "kind": "TASK"},
            {"node_id": "EVID-1", "kind": "EVIDENCE"},
        ],
        "edges": [
            {"source_id": "DESIGN-1", "target_id": "INTENT-1", "relation": "DERIVED_FROM"},
            {"source_id": "TASK-1", "target_id": "CRIT-1", "relation": "IMPLEMENTS"},
            {"source_id": "EVID-1", "target_id": "TASK-1", "relation": "VERIFIES"},
        ]
    }
    lineage = validate_lineage(payload)
    assert len(lineage.nodes) == 5
    assert len(lineage.edges) == 3

def test_duplicate_node_id_fails():
    payload = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "INTENT-1", "kind": "INTENT"},
            {"node_id": "INTENT-1", "kind": "TASK"},
        ],
        "edges": []
    }
    with pytest.raises(LineageValidationError) as excinfo:
        validate_lineage(payload)
    assert excinfo.value.code == "DUPLICATE_NODE_ID"

def test_dangling_edge_fails():
    payload = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "INTENT-1", "kind": "INTENT"},
        ],
        "edges": [
            {"source_id": "DESIGN-1", "target_id": "INTENT-1", "relation": "DERIVED_FROM"},
        ]
    }
    with pytest.raises(LineageValidationError) as excinfo:
        validate_lineage(payload)
    assert excinfo.value.code == "DANGLING_EDGE"

def test_invalid_node_kind_fails():
    payload = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "INTENT-1", "kind": "UNKNOWN_KIND"},
        ],
        "edges": []
    }
    with pytest.raises(LineageValidationError) as excinfo:
        validate_lineage(payload)
    assert excinfo.value.code == "INVALID_NODE_KIND"

def test_invalid_relation_kind_fails():
    payload = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "INTENT-1", "kind": "INTENT"},
            {"node_id": "DESIGN-1", "kind": "DESIGN"},
        ],
        "edges": [
            {"source_id": "DESIGN-1", "target_id": "INTENT-1", "relation": "UNKNOWN_REL"},
        ]
    }
    with pytest.raises(LineageValidationError) as excinfo:
        validate_lineage(payload)
    assert excinfo.value.code == "INVALID_RELATION_KIND"

def test_invalid_relation_direction_fails():
    payload = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "INTENT-1", "kind": "INTENT"},
            {"node_id": "DESIGN-1", "kind": "DESIGN"},
        ],
        "edges": [
            {"source_id": "INTENT-1", "target_id": "DESIGN-1", "relation": "DERIVED_FROM"},
        ]
    }
    with pytest.raises(LineageValidationError) as excinfo:
        validate_lineage(payload)
    assert excinfo.value.code == "INVALID_RELATION_DIRECTION"

def test_unintended_cycle_fails():
    payload = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "INTENT-1", "kind": "INTENT"},
            {"node_id": "INTENT-2", "kind": "INTENT"},
        ],
        "edges": [
            {"source_id": "INTENT-1", "target_id": "INTENT-2", "relation": "SUPERSEDES"},
            {"source_id": "INTENT-2", "target_id": "INTENT-1", "relation": "SUPERSEDES"},
        ]
    }
    with pytest.raises(LineageValidationError) as excinfo:
        validate_lineage(payload)
    assert excinfo.value.code == "UNINTENDED_CYCLE"

def test_complete_trace_criterion_to_task_to_evidence():
    payload = {
        "schema_version": 1,
        "nodes": [
            {"node_id": "CRIT-1", "kind": "CRITERION"},
            {"node_id": "TASK-1", "kind": "TASK"},
            {"node_id": "EVID-1", "kind": "EVIDENCE"},
        ],
        "edges": [
            {"source_id": "TASK-1", "target_id": "CRIT-1", "relation": "IMPLEMENTS"},
            {"source_id": "EVID-1", "target_id": "TASK-1", "relation": "VERIFIES"},
        ]
    }
    validate_lineage(payload)
