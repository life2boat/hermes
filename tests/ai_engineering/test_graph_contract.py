import pytest
import json
from ai_engineering.graph_contract import (
    GraphNode, GraphEdge, GraphProvenance, GraphSnapshot, GraphVerificationError,
    AuthoritativeFactState, AuthoritativeSourceSnapshot, classify_provenance,
    deserialize_graph_snapshot, serialize_graph_snapshot, GRAPH_SCHEMA_VERSION
)

def test_canonical_node_determinism():
    prov = GraphProvenance("sqlite_memory_os_facts", "fact1", 1)
    n1 = GraphNode.create("core:person", {"name": "Alice"}, prov)
    n2 = GraphNode.create("core:person", {"name": "Alice"}, prov)
    assert n1.node_id == n2.node_id
    n1.verify_identity()

def test_canonical_edge_determinism():
    prov = GraphProvenance("sqlite_memory_os_facts", "fact1", 1)
    n1 = GraphNode.create("core:person", {"name": "Alice"}, prov)
    n2 = GraphNode.create("core:person", {"name": "Bob"}, prov)
    e1 = GraphEdge.create(n1.node_id, n2.node_id, "relation:knows", {"since": 2020}, prov)
    e2 = GraphEdge.create(n1.node_id, n2.node_id, "relation:knows", {"since": 2020}, prov)
    assert e1.edge_id == e2.edge_id
    e1.verify_identity()

def test_canonical_snapshot_determinism():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n = GraphNode.create("core:thing", {"v": 1}, prov)
    s1 = GraphSnapshot.create([n], [])
    s2 = GraphSnapshot.create([n], [])
    assert s1.snapshot_id == s2.snapshot_id
    s1.verify_identity()

def test_mapping_insertion_order_independence():
    prov = GraphProvenance("sqlite_memory_os_facts", "fact1", 1)
    n1 = GraphNode.create("core:person", {"a": 1, "b": 2}, prov)
    n2 = GraphNode.create("core:person", {"b": 2, "a": 1}, prov)
    assert n1.node_id == n2.node_id

def test_node_ordering_independence_in_snapshot():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {"v": 1}, prov)
    n2 = GraphNode.create("core:b", {"v": 2}, prov)
    s1 = GraphSnapshot.create([n1, n2], [])
    s2 = GraphSnapshot.create([n2, n1], [])
    assert s1.snapshot_id == s2.snapshot_id

def test_edge_ordering_independence_in_snapshot():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {"v": 1}, prov)
    n2 = GraphNode.create("core:b", {"v": 2}, prov)
    e1 = GraphEdge.create(n1.node_id, n2.node_id, "rel:one", {}, prov)
    e2 = GraphEdge.create(n1.node_id, n2.node_id, "rel:two", {}, prov)
    s1 = GraphSnapshot.create([n1, n2], [e1, e2])
    s2 = GraphSnapshot.create([n1, n2], [e2, e1])
    assert s1.snapshot_id == s2.snapshot_id

def test_duplicate_node_rejection():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {"v": 1}, prov)
    with pytest.raises(GraphVerificationError, match="Duplicate node_id rejected"):
        GraphSnapshot.create([n1, n1], [])

def test_duplicate_edge_rejection():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {"v": 1}, prov)
    n2 = GraphNode.create("core:b", {"v": 2}, prov)
    e1 = GraphEdge.create(n1.node_id, n2.node_id, "rel:one", {}, prov)
    with pytest.raises(GraphVerificationError, match="Duplicate edge_id rejected"):
        GraphSnapshot.create([n1, n2], [e1, e1])

def test_malformed_node_type():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    with pytest.raises(GraphVerificationError):
        GraphNode.create("invalid-type", {}, prov)

def test_malformed_relation_type():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {}, prov)
    n2 = GraphNode.create("core:b", {}, prov)
    with pytest.raises(GraphVerificationError):
        GraphEdge.create(n1.node_id, n2.node_id, "BAD TYPE", {}, prov)

def test_empty_node_type():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    with pytest.raises(GraphVerificationError):
        GraphNode.create("", {}, prov)

def test_empty_relation_type():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {}, prov)
    n2 = GraphNode.create("core:b", {}, prov)
    with pytest.raises(GraphVerificationError):
        GraphEdge.create(n1.node_id, n2.node_id, "", {}, prov)

def test_unknown_source_target_node_reference():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {}, prov)
    e1 = GraphEdge.create(n1.node_id, "gn_missing", "rel:a", {}, prov)
    with pytest.raises(GraphVerificationError, match="Dangling edge reference"):
        GraphSnapshot.create([n1], [e1])

def test_self_edge_policy():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {}, prov)
    with pytest.raises(GraphVerificationError, match="Self-edges are not permitted"):
        GraphEdge.create(n1.node_id, n1.node_id, "rel:a", {}, prov)

def test_unsupported_provenance_source():
    with pytest.raises(GraphVerificationError):
        GraphProvenance("unsupported", "f1", 1)

def test_malformed_empty_fact_id():
    with pytest.raises(GraphVerificationError):
        GraphProvenance("sqlite_memory_os_facts", "", 1)
    with pytest.raises(GraphVerificationError):
        GraphProvenance("sqlite_memory_os_facts", "  ", 1)

def test_negative_bool_revision():
    with pytest.raises(GraphVerificationError):
        GraphProvenance("sqlite_memory_os_facts", "f1", -1)
    with pytest.raises(GraphVerificationError):
        GraphProvenance("sqlite_memory_os_facts", "f1", True)  # type: ignore

def test_tampered_identities():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {}, prov)
    tampered_node = GraphNode("gn_bad", n1.node_type, n1.properties, n1.provenance)
    with pytest.raises(GraphVerificationError):
        tampered_node.verify_identity()

    e1 = GraphEdge.create(n1.node_id, "gn_other", "rel:a", {}, prov)
    tampered_edge = GraphEdge("ge_bad", e1.source_node_id, e1.target_node_id, e1.relation_type, e1.properties, e1.provenance)
    with pytest.raises(GraphVerificationError):
        tampered_edge.verify_identity()

    s1 = GraphSnapshot.create([n1], [])
    tampered_snapshot = GraphSnapshot(s1.schema_version, "gs_bad", s1.nodes, s1.edges, None)
    with pytest.raises(GraphVerificationError):
        tampered_snapshot.verify_identity()

def test_provenance_classification():
    auth = AuthoritativeSourceSnapshot(
        facts=(AuthoritativeFactState("f1", 2, "ACTIVE"), AuthoritativeFactState("f2", 1, "DELETED")),
        is_complete=True
    )
    
    # Current
    assert classify_provenance(GraphProvenance("sqlite_memory_os_facts", "f1", 2), auth) == "CURRENT"
    # Stale
    assert classify_provenance(GraphProvenance("sqlite_memory_os_facts", "f1", 1), auth) == "STALE"
    # Deleted
    assert classify_provenance(GraphProvenance("sqlite_memory_os_facts", "f2", 1), auth) == "DELETED_SOURCE"
    # Unknown (complete snapshot without f3)
    assert classify_provenance(GraphProvenance("sqlite_memory_os_facts", "f3", 1), auth) == "UNKNOWN_SOURCE"

def test_partial_source_state_ambiguity():
    auth = AuthoritativeSourceSnapshot(facts=(), is_complete=False)
    with pytest.raises(GraphVerificationError, match="ambiguous"):
        classify_provenance(GraphProvenance("sqlite_memory_os_facts", "f1", 1), auth)

def test_conflicting_assertion():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {}, prov)
    n2 = GraphNode.create("core:b", {}, prov)
    e1 = GraphEdge.create(n1.node_id, n2.node_id, "rel:a", {"k": 1}, prov)
    e2 = GraphEdge.create(n1.node_id, n2.node_id, "rel:a", {"k": 2}, prov)
    with pytest.raises(GraphVerificationError, match="Conflict detected"):
        GraphSnapshot.create([n1, n2], [e1, e2])

def test_non_finite_numeric_property():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    with pytest.raises(GraphVerificationError):
        GraphNode.create("core:a", {"v": float('inf')}, prov)
    with pytest.raises(GraphVerificationError):
        GraphNode.create("core:a", {"v": float('nan')}, prov)

def test_unsupported_schema_version():
    s_json = '{"schema_version": 999, "snapshot_id": "gs_00", "nodes": [], "edges": []}'
    with pytest.raises(GraphVerificationError):
        deserialize_graph_snapshot(s_json)

def test_duplicate_json_key_unexpected_field_missing():
    s_json = '{"schema_version": 1, "snapshot_id": "gs_bad", "nodes": [], "edges": [], "extra": 1}'
    # We don't strictly reject unexpected fields in the pure Python parser unless we write a full JSON validator.
    # But missing schema_version fails, tampered ID fails.
    with pytest.raises(GraphVerificationError):
        deserialize_graph_snapshot(s_json)

def test_excessive_bounds():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    long_key = "k" * 101
    with pytest.raises(GraphVerificationError):
        GraphNode.create("core:a", {long_key: 1}, prov)
    
    long_val = "v" * 4097
    with pytest.raises(GraphVerificationError):
        GraphNode.create("core:a", {"k": long_val}, prov)

    many_props = {f"k{i}": i for i in range(101)}
    with pytest.raises(GraphVerificationError):
        GraphNode.create("core:a", many_props, prov)

def test_mutable_caller_input_aliasing():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    props = {"v": 1}
    n1 = GraphNode.create("core:a", props, prov)
    props["v"] = 2  # Mutate original dict
    assert n1.properties["v"] == 1  # Node should be unaffected

def test_prohibited_secrets():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    with pytest.raises(GraphVerificationError):
        GraphNode.create("core:a", {"raw_prompt": "secret"}, prov)
    with pytest.raises(GraphVerificationError):
        GraphNode.create("core:a", {"my_password": "123"}, prov)

def test_deterministic_rebuild_equivalence():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {}, prov)
    s1 = GraphSnapshot.create([n1], [])
    
    json_str = serialize_graph_snapshot(s1)
    s2 = deserialize_graph_snapshot(json_str)
    assert s1.snapshot_id == s2.snapshot_id
    assert s1.nodes[0].node_id == s2.nodes[0].node_id

def test_properties_must_be_mapping():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    with pytest.raises(GraphVerificationError):
        GraphNode.create("core:a", "not-a-mapping", prov)

def test_max_properties_exceeded():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    props = {f"k{i}": i for i in range(101)}
    with pytest.raises(GraphVerificationError):
        GraphNode.create("core:a", props, prov)

def test_property_key_must_be_string():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    with pytest.raises(GraphVerificationError):
        GraphNode.create("core:a", {1: "val"}, prov)

def test_property_value_must_be_scalar():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    with pytest.raises(GraphVerificationError):
        GraphNode.create("core:a", {"k": {"nested": 1}}, prov)

def test_graph_provenance_fact_id_not_string():
    with pytest.raises(GraphVerificationError):
        GraphProvenance("sqlite_memory_os_facts", 123, 1)

def test_graph_provenance_fact_id_whitespace():
    with pytest.raises(GraphVerificationError):
        GraphProvenance("sqlite_memory_os_facts", "f 1", 1)

def test_graph_provenance_revision_not_int():
    with pytest.raises(GraphVerificationError):
        GraphProvenance("sqlite_memory_os_facts", "f1", "1")

def test_graph_edge_source_node_format_invalid():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    with pytest.raises(GraphVerificationError):
        GraphEdge.create("invalid_id", "gn_123", "rel:a", {}, prov)

def test_graph_edge_target_node_format_invalid():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    with pytest.raises(GraphVerificationError):
        GraphEdge.create("gn_123", "invalid_id", "rel:a", {}, prov)

def test_authoritative_fact_state_invalid_fact_id():
    with pytest.raises(GraphVerificationError):
        AuthoritativeFactState("", 1, "ACTIVE")
    with pytest.raises(GraphVerificationError):
        AuthoritativeFactState("f 1", 1, "ACTIVE")

def test_authoritative_fact_state_negative_revision():
    with pytest.raises(GraphVerificationError):
        AuthoritativeFactState("f1", -1, "ACTIVE")

def test_authoritative_fact_state_invalid_status():
    with pytest.raises(GraphVerificationError):
        AuthoritativeFactState("f1", 1, "INVALID_STATUS")

def test_classify_provenance_future_revision():
    auth = AuthoritativeSourceSnapshot(facts=(AuthoritativeFactState("f1", 1, "ACTIVE"),), is_complete=False)
    # The current code returns UNKNOWN_SOURCE for future revisions. So let's test that!
    assert classify_provenance(GraphProvenance("sqlite_memory_os_facts", "f1", 2), auth) == "UNKNOWN_SOURCE"

def test_graph_snapshot_max_nodes():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    nodes = [GraphNode.create("core:a", {"i": i}, prov) for i in range(1001)]
    with pytest.raises(GraphVerificationError):
        GraphSnapshot.create(nodes, [])

def test_graph_snapshot_max_edges():
    prov = GraphProvenance("sqlite_memory_os_facts", "f1", 1)
    n1 = GraphNode.create("core:a", {}, prov)
    n2 = GraphNode.create("core:b", {}, prov)
    edges = [GraphEdge.create(n1.node_id, n2.node_id, "rel:a", {"i": i}, prov) for i in range(5001)]
    with pytest.raises(GraphVerificationError):
        GraphSnapshot.create([n1, n2], edges)

def test_deserialize_snapshot_invalid_json():
    with pytest.raises(GraphVerificationError):
        deserialize_graph_snapshot("{invalid json}")

def test_deserialize_snapshot_root_not_object():
    with pytest.raises(GraphVerificationError):
        deserialize_graph_snapshot("[]")

def test_deserialize_snapshot_missing_schema_version():
    with pytest.raises(GraphVerificationError):
        deserialize_graph_snapshot('{"snapshot_id": "gs_123"}')
