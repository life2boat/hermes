import pytest
from typing import Any
from ai_engineering.graph_contract import (
    GraphNode,
    GraphEdge,
    GraphProvenance,
    GraphVerificationError,
    GRAPH_SCHEMA_VERSION,
)

def test_canonical_determinism_and_ordering():
    prov = GraphProvenance("sqlite_memory_os_facts", "fact_123", 1)

    # Different insertion ordering for properties must yield the same canonical ID
    props_a = {"alpha": 1, "beta": "two"}
    props_b = {"beta": "two", "alpha": 1}

    node_a = GraphNode.create("Person", props_a, prov)
    node_b = GraphNode.create("Person", props_b, prov)

    assert node_a.node_id == node_b.node_id
    assert node_a.node_id.startswith("gn_")

    # Edge determinism
    edge_a = GraphEdge.create(node_a.node_id, "gn_other", "KNOWS", props_a, prov)
    edge_b = GraphEdge.create(node_a.node_id, "gn_other", "KNOWS", props_b, prov)

    assert edge_a.edge_id == edge_b.edge_id
    assert edge_a.edge_id.startswith("ge_")

def test_self_edge_policy():
    prov = GraphProvenance("sqlite_memory_os_facts", "fact_123", 1)

    with pytest.raises(GraphVerificationError, match="Self-edges are not permitted"):
        GraphEdge.create("gn_same", "gn_same", "KNOWS", {}, prov)

def test_tampered_node_id():
    prov = GraphProvenance("sqlite_memory_os_facts", "fact_123", 1)
    node = GraphNode.create("Person", {"name": "Alice"}, prov)

    tampered_node = GraphNode(
        node_id="gn_tampered123",
        node_type=node.node_type,
        properties=node.properties,
        provenance=node.provenance
    )

    with pytest.raises(GraphVerificationError, match="Tampered or non-deterministic node identity"):
        tampered_node.verify_identity()

def test_tampered_edge_id():
    prov = GraphProvenance("sqlite_memory_os_facts", "fact_123", 1)
    edge = GraphEdge.create("gn_a", "gn_b", "KNOWS", {}, prov)

    tampered_edge = GraphEdge(
        edge_id="ge_tampered123",
        source_node_id=edge.source_node_id,
        target_node_id=edge.target_node_id,
        relation_type=edge.relation_type,
        properties=edge.properties,
        provenance=edge.provenance
    )

    with pytest.raises(GraphVerificationError, match="Tampered or non-deterministic edge identity"):
        tampered_edge.verify_identity()

def test_edge_tampered_self_edge():
    prov = GraphProvenance("sqlite_memory_os_facts", "fact_123", 1)
    tampered_edge = GraphEdge(
        edge_id="ge_123",
        source_node_id="gn_a",
        target_node_id="gn_a",  # self-edge tampered after creation
        relation_type="KNOWS",
        properties={},
        provenance=prov
    )
    with pytest.raises(GraphVerificationError, match="Self-edges are not permitted"):
        tampered_edge.verify_identity()

def test_non_finite_values_rejected():
    prov = GraphProvenance("sqlite_memory_os_facts", "fact_123", 1)

    # JSON standard does not support NaN or Infinity
    # allow_nan=False should raise ValueError which is caught and re-raised as GraphVerificationError
    with pytest.raises(GraphVerificationError, match="Non-canonical property value"):
        GraphNode.create("Metric", {"value": float("inf")}, prov)

    with pytest.raises(GraphVerificationError, match="Non-canonical property value"):
        GraphNode.create("Metric", {"value": float("nan")}, prov)

def test_secret_like_material_rejected():
    prov = GraphProvenance("sqlite_memory_os_facts", "fact_123", 1)

    bad_props = [
        {"API_KEY": "12345"},
        {"user_password": "xyz"},
        {"token": "abc"},
        {"aws_secret_access_key": "def"},
        {"db_credential": "ghi"}
    ]

    for props in bad_props:
        with pytest.raises(GraphVerificationError, match="Secret-like material rejected"):
            GraphNode.create("Config", props, prov)

        with pytest.raises(GraphVerificationError, match="Secret-like material rejected"):
            GraphEdge.create("gn_a", "gn_b", "HAS_CONFIG", props, prov)

def test_canonical_dict():
    prov = GraphProvenance("sqlite", 100, 2)
    node = GraphNode.create("Test", {"k": "v"}, prov)

    d = node.canonical_dict()
    assert d["node_type"] == "Test"
    assert d["properties"] == {"k": "v"}
    assert d["provenance"] == {"source_system": "sqlite", "fact_id": "100", "revision": 2}

    edge = GraphEdge.create("gn_a", "gn_b", "REL", {"k": "v"}, prov)
    ed = edge.canonical_dict()
    assert ed["relation_type"] == "REL"
    assert ed["source_node_id"] == "gn_a"
    assert ed["target_node_id"] == "gn_b"
