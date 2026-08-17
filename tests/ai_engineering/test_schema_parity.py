import json
from pathlib import Path
import pytest
import jsonschema

from ai_engineering.graph_contract import (
    deserialize_graph_snapshot, serialize_graph_snapshot, GraphVerificationError, GRAPH_SCHEMA_VERSION,
    GraphSnapshot, GraphNode, GraphEdge, GraphProvenance, AuthoritativeSourceSnapshot, AuthoritativeFactState
)

SCHEMA_PATH = Path(__file__).parent.parent.parent / "schemas" / "memory-graph-contract-v1.schema.json"

@pytest.fixture
def graph_schema():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

@pytest.fixture
def valid_payload():
    return {
        "schema_version": 1,
        "snapshot_id": "gs_" + "a" * 64,
        "nodes": [
            {
                "node_id": "gn_" + "b" * 64,
                "node_type": "core:person",
                "properties": {"name": "Alice"},
                "provenance": {
                    "source_system": "sqlite_memory_os_facts",
                    "fact_id": "f1",
                    "revision": 1
                }
            }
        ],
        "edges": [],
        "authoritative_source": {
            "facts": [
                {
                    "fact_id": "f1",
                    "current_revision": 1,
                    "status": "ACTIVE"
                }
            ],
            "is_complete": True
        }
    }

def test_schema_meta_validation(graph_schema):
    # Ensure the schema itself is a valid Draft202012 schema
    jsonschema.Draft202012Validator.check_schema(graph_schema)

def test_valid_payload_parity(graph_schema, valid_payload):
    # Schema PASS
    jsonschema.validate(instance=valid_payload, schema=graph_schema)
    
    # Python PASS
    # To pass python we need valid crypto hashes. So we can just create a real snapshot and serialize it,
    # then check that it passes schema validation too!
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n = GraphNode.create("core:person", {"name": "Alice"}, prov)
    auth = AuthoritativeSourceSnapshot((AuthoritativeFactState("f1", 1, "ACTIVE"),), True)
    snap = GraphSnapshot.create([n], [], auth)
    
    real_payload = json.loads(serialize_graph_snapshot(snap))
    
    # Schema PASS on real python output
    jsonschema.validate(instance=real_payload, schema=graph_schema)
    
    # Python PASS on real python output
    deserialize_graph_snapshot(json.dumps(real_payload))

def test_extra_field_rejected(graph_schema, valid_payload):
    payload = valid_payload.copy()
    payload["unknown_field"] = "bad"
    
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=graph_schema)
        
    with pytest.raises(GraphVerificationError):
        deserialize_graph_snapshot(json.dumps(payload))

def test_bad_node_id_rejected(graph_schema, valid_payload):
    payload = valid_payload.copy()
    payload["nodes"][0]["node_id"] = "bad_id"
    
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=graph_schema)
        
    with pytest.raises(GraphVerificationError):
        deserialize_graph_snapshot(json.dumps(payload))

def test_missing_required_field_rejected(graph_schema, valid_payload):
    payload = valid_payload.copy()
    del payload["nodes"][0]["node_type"]
    
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=graph_schema)
        
    with pytest.raises(GraphVerificationError):
        deserialize_graph_snapshot(json.dumps(payload))

def test_bad_revision_rejected(graph_schema, valid_payload):
    payload = valid_payload.copy()
    payload["nodes"][0]["provenance"]["revision"] = -1
    
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=graph_schema)
        
    with pytest.raises(GraphVerificationError):
        deserialize_graph_snapshot(json.dumps(payload))

def test_too_many_nodes_rejected(graph_schema, valid_payload):
    payload = valid_payload.copy()
    payload["nodes"] = [payload["nodes"][0]] * 1001  # Duplicate node ID will fail python later, but we check max nodes first
    
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=graph_schema)
        
    with pytest.raises(GraphVerificationError):
        deserialize_graph_snapshot(json.dumps(payload))

def test_tampered_id_rejected(graph_schema):
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n = GraphNode.create("core:person", {"name": "Alice"}, prov)
    auth = AuthoritativeSourceSnapshot((AuthoritativeFactState("f1", 1, "ACTIVE"),), True)
    snap = GraphSnapshot.create([n], [], auth)
    
    real_payload = json.loads(serialize_graph_snapshot(snap))
    
    # Tamper the node properties without updating ID
    real_payload["nodes"][0]["properties"]["name"] = "Bob"
    
    # Schema PASSES because schema doesn't verify crypto hashes
    jsonschema.validate(instance=real_payload, schema=graph_schema)
    
    # Python FAILS
    with pytest.raises(GraphVerificationError, match="Tampered"):
        deserialize_graph_snapshot(json.dumps(real_payload))
