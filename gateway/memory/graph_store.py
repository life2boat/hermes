"""
gateway/memory/graph_store.py
Persistent derived graph storage for the Hermes Memory system.
"""

from __future__ import annotations

import json
import sqlite3
from enum import Enum
from typing import Any

from ai_engineering.graph_contract import (
    GRAPH_SCHEMA_VERSION,
    GraphSnapshot,
    deserialize_graph_snapshot,
    serialize_graph_snapshot,
    GraphProvenance,
    GraphNode,
    GraphEdge,
    MappingProxyType,
    GraphVerificationError,
)
from gateway.memory.graph_projection import (
    GraphProjectionResult,
    ProjectionExclusion,
    verify_graph_projection_result,
    read_authoritative_memory_facts,
    project_authoritative_memory_facts,
    MEMORY_GRAPH_PROJECTION_VERSION,
    ProjectionError,
)

MEMORY_GRAPH_STORE_SCHEMA_VERSION = 1
MEMORY_GRAPH_STORE_MIGRATION_ID = "memory-graph-store-schema-v1"


class GraphStoreSchemaClassification(str, Enum):
    ABSENT = "ABSENT"
    KNOWN_COMPATIBLE_PARTIAL = "KNOWN_COMPATIBLE_PARTIAL"
    CURRENT = "CURRENT"
    INCOMPATIBLE = "INCOMPATIBLE"


class GraphStoreError(ValueError):
    pass


_CREATE_META = """
CREATE TABLE IF NOT EXISTS memory_graph_store_meta (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    schema_version INTEGER NOT NULL
)
"""

_CREATE_USER_STATE = """
CREATE TABLE IF NOT EXISTS memory_graph_user_state (
    user_id INTEGER PRIMARY KEY,
    graph_schema_version INTEGER NOT NULL,
    projection_version INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL,
    projection_id TEXT NOT NULL,
    canonical_snapshot_json TEXT NOT NULL,
    input_fact_count INTEGER NOT NULL,
    projected_fact_count INTEGER NOT NULL,
    excluded_fact_count INTEGER NOT NULL
)
"""

_CREATE_NODES = """
CREATE TABLE IF NOT EXISTS memory_graph_nodes (
    user_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    properties_json TEXT NOT NULL,
    primary_provenance_fact_id TEXT NOT NULL,
    primary_provenance_revision INTEGER NOT NULL,
    PRIMARY KEY (user_id, node_id),
    FOREIGN KEY (user_id) REFERENCES memory_graph_user_state(user_id) ON DELETE CASCADE
)
"""

_CREATE_EDGES = """
CREATE TABLE IF NOT EXISTS memory_graph_edges (
    user_id INTEGER NOT NULL,
    edge_id TEXT NOT NULL,
    source_node_id TEXT NOT NULL,
    target_node_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    properties_json TEXT NOT NULL,
    primary_provenance_fact_id TEXT NOT NULL,
    primary_provenance_revision INTEGER NOT NULL,
    PRIMARY KEY (user_id, edge_id),
    FOREIGN KEY (user_id) REFERENCES memory_graph_user_state(user_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, source_node_id) REFERENCES memory_graph_nodes(user_id, node_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, target_node_id) REFERENCES memory_graph_nodes(user_id, node_id) ON DELETE CASCADE
)
"""

_CREATE_NODE_SUPPORTS = """
CREATE TABLE IF NOT EXISTS memory_graph_node_supports (
    user_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (user_id, node_id, fact_id, revision),
    FOREIGN KEY (user_id) REFERENCES memory_graph_user_state(user_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, node_id) REFERENCES memory_graph_nodes(user_id, node_id) ON DELETE CASCADE
)
"""

_CREATE_EDGE_SUPPORTS = """
CREATE TABLE IF NOT EXISTS memory_graph_edge_supports (
    user_id INTEGER NOT NULL,
    edge_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (user_id, edge_id, fact_id, revision),
    FOREIGN KEY (user_id) REFERENCES memory_graph_user_state(user_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, edge_id) REFERENCES memory_graph_edges(user_id, edge_id) ON DELETE CASCADE
)
"""

_CREATE_EXCLUSIONS = """
CREATE TABLE IF NOT EXISTS memory_graph_exclusions (
    user_id INTEGER NOT NULL,
    fact_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (user_id, fact_id, reason),
    FOREIGN KEY (user_id) REFERENCES memory_graph_user_state(user_id) ON DELETE CASCADE
)
"""


def classify_memory_graph_store_schema(
    conn: sqlite3.Connection,
) -> GraphStoreSchemaClassification:
    cur = conn.cursor()

    expected_tables = {
        "memory_graph_store_meta": {
            "columns": {
                "singleton_id": {"type": "INTEGER", "notnull": 0, "pk": 1},
                "schema_version": {"type": "INTEGER", "notnull": 1, "pk": 0},
            },
            "fks": [],
            "sql_like": "CHECK(singleton_id = 1)",
        },
        "memory_graph_user_state": {
            "columns": {
                "user_id": {"type": "INTEGER", "notnull": 0, "pk": 1},
                "graph_schema_version": {"type": "INTEGER", "notnull": 1, "pk": 0},
                "projection_version": {"type": "INTEGER", "notnull": 1, "pk": 0},
                "snapshot_id": {"type": "TEXT", "notnull": 1, "pk": 0},
                "projection_id": {"type": "TEXT", "notnull": 1, "pk": 0},
                "canonical_snapshot_json": {"type": "TEXT", "notnull": 1, "pk": 0},
                "input_fact_count": {"type": "INTEGER", "notnull": 1, "pk": 0},
                "projected_fact_count": {"type": "INTEGER", "notnull": 1, "pk": 0},
                "excluded_fact_count": {"type": "INTEGER", "notnull": 1, "pk": 0},
            },
            "fks": [],
        },
        "memory_graph_nodes": {
            "columns": {
                "user_id": {"type": "INTEGER", "notnull": 1, "pk": 1},
                "node_id": {"type": "TEXT", "notnull": 1, "pk": 2},
                "node_type": {"type": "TEXT", "notnull": 1, "pk": 0},
                "properties_json": {"type": "TEXT", "notnull": 1, "pk": 0},
                "primary_provenance_fact_id": {"type": "TEXT", "notnull": 1, "pk": 0},
                "primary_provenance_revision": {
                    "type": "INTEGER",
                    "notnull": 1,
                    "pk": 0,
                },
            },
            "fks": [
                {
                    "from": ["user_id"],
                    "table": "memory_graph_user_state",
                    "to": ["user_id"],
                    "on_delete": "CASCADE",
                },
            ],
        },
        "memory_graph_edges": {
            "columns": {
                "user_id": {"type": "INTEGER", "notnull": 1, "pk": 1},
                "edge_id": {"type": "TEXT", "notnull": 1, "pk": 2},
                "source_node_id": {"type": "TEXT", "notnull": 1, "pk": 0},
                "target_node_id": {"type": "TEXT", "notnull": 1, "pk": 0},
                "relation_type": {"type": "TEXT", "notnull": 1, "pk": 0},
                "properties_json": {"type": "TEXT", "notnull": 1, "pk": 0},
                "primary_provenance_fact_id": {"type": "TEXT", "notnull": 1, "pk": 0},
                "primary_provenance_revision": {
                    "type": "INTEGER",
                    "notnull": 1,
                    "pk": 0,
                },
            },
            "fks": [
                {
                    "from": ["user_id"],
                    "table": "memory_graph_user_state",
                    "to": ["user_id"],
                    "on_delete": "CASCADE",
                },
                {
                    "from": ["user_id", "source_node_id"],
                    "table": "memory_graph_nodes",
                    "to": ["user_id", "node_id"],
                    "on_delete": "CASCADE",
                },
                {
                    "from": ["user_id", "target_node_id"],
                    "table": "memory_graph_nodes",
                    "to": ["user_id", "node_id"],
                    "on_delete": "CASCADE",
                },
            ],
        },
        "memory_graph_node_supports": {
            "columns": {
                "user_id": {"type": "INTEGER", "notnull": 1, "pk": 1},
                "node_id": {"type": "TEXT", "notnull": 1, "pk": 2},
                "fact_id": {"type": "TEXT", "notnull": 1, "pk": 3},
                "revision": {"type": "INTEGER", "notnull": 1, "pk": 4},
            },
            "fks": [
                {
                    "from": ["user_id"],
                    "table": "memory_graph_user_state",
                    "to": ["user_id"],
                    "on_delete": "CASCADE",
                },
                {
                    "from": ["user_id", "node_id"],
                    "table": "memory_graph_nodes",
                    "to": ["user_id", "node_id"],
                    "on_delete": "CASCADE",
                },
            ],
        },
        "memory_graph_edge_supports": {
            "columns": {
                "user_id": {"type": "INTEGER", "notnull": 1, "pk": 1},
                "edge_id": {"type": "TEXT", "notnull": 1, "pk": 2},
                "fact_id": {"type": "TEXT", "notnull": 1, "pk": 3},
                "revision": {"type": "INTEGER", "notnull": 1, "pk": 4},
            },
            "fks": [
                {
                    "from": ["user_id"],
                    "table": "memory_graph_user_state",
                    "to": ["user_id"],
                    "on_delete": "CASCADE",
                },
                {
                    "from": ["user_id", "edge_id"],
                    "table": "memory_graph_edges",
                    "to": ["user_id", "edge_id"],
                    "on_delete": "CASCADE",
                },
            ],
        },
        "memory_graph_exclusions": {
            "columns": {
                "user_id": {"type": "INTEGER", "notnull": 1, "pk": 1},
                "fact_id": {"type": "TEXT", "notnull": 1, "pk": 2},
                "reason": {"type": "TEXT", "notnull": 1, "pk": 3},
            },
            "fks": [
                {
                    "from": ["user_id"],
                    "table": "memory_graph_user_state",
                    "to": ["user_id"],
                    "on_delete": "CASCADE",
                },
            ],
        },
    }

    try:
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memory_graph_%'"
        )
        existing_tables = {row[0] for row in cur.fetchall()}

        if not existing_tables:
            return GraphStoreSchemaClassification.ABSENT

        if not existing_tables.issubset(set(expected_tables.keys())):
            return GraphStoreSchemaClassification.INCOMPATIBLE

        for table in existing_tables:
            schema = expected_tables[table]
            cur.execute(f"PRAGMA table_info({table})")
            columns_info = cur.fetchall()
            if not columns_info:
                return GraphStoreSchemaClassification.INCOMPATIBLE

            columns = {
                row[1]: {"type": row[2], "notnull": row[3], "pk": row[5]}
                for row in columns_info
            }
            if set(columns.keys()) != set(schema["columns"].keys()):
                return GraphStoreSchemaClassification.INCOMPATIBLE
            for col, cinfo in schema["columns"].items():
                if col not in columns:
                    return GraphStoreSchemaClassification.INCOMPATIBLE
                if columns[col]["type"] != cinfo["type"]:
                    return GraphStoreSchemaClassification.INCOMPATIBLE
                if columns[col]["notnull"] != cinfo["notnull"]:
                    return GraphStoreSchemaClassification.INCOMPATIBLE
                if columns[col]["pk"] != cinfo["pk"]:
                    return GraphStoreSchemaClassification.INCOMPATIBLE

            if "sql_like" in schema:
                cur.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                sql = cur.fetchone()[0]
                if schema["sql_like"].lower() not in sql.lower().replace(" ", ""):
                    # check canonical schema identity
                    if "singleton_id = 1" not in sql:
                        return GraphStoreSchemaClassification.INCOMPATIBLE

            cur.execute(f"PRAGMA foreign_key_list({table})")
            fks_info = cur.fetchall()

            # map fk info by id
            actual_fks = {}
            for row in fks_info:
                fk_id = row[0]
                if fk_id not in actual_fks:
                    actual_fks[fk_id] = {
                        "table": row[2],
                        "from": [],
                        "to": [],
                        "on_delete": row[6],
                    }
                actual_fks[fk_id]["from"].append(row[3])
                actual_fks[fk_id]["to"].append(row[4])

            expected_fks = schema.get("fks", [])
            if len(actual_fks) != len(expected_fks):
                return GraphStoreSchemaClassification.INCOMPATIBLE

            for expected_fk in expected_fks:
                matched = False
                for actual_fk in actual_fks.values():
                    if (
                        actual_fk["table"] == expected_fk["table"]
                        and actual_fk["from"] == expected_fk["from"]
                        and actual_fk["to"] == expected_fk["to"]
                        and actual_fk["on_delete"] == expected_fk["on_delete"]
                    ):
                        matched = True
                        break
                if not matched:
                    return GraphStoreSchemaClassification.INCOMPATIBLE

        if "memory_graph_store_meta" in existing_tables:
            cur.execute(
                "SELECT schema_version FROM memory_graph_store_meta WHERE singleton_id = 1"
            )
            row = cur.fetchone()
            if not row or row[0] != MEMORY_GRAPH_STORE_SCHEMA_VERSION:
                return GraphStoreSchemaClassification.INCOMPATIBLE

        if existing_tables != set(expected_tables.keys()):
            return GraphStoreSchemaClassification.KNOWN_COMPATIBLE_PARTIAL

        return GraphStoreSchemaClassification.CURRENT
    except sqlite3.Error:
        return GraphStoreSchemaClassification.INCOMPATIBLE


def validate_memory_graph_store_schema(conn: sqlite3.Connection) -> None:
    classification = classify_memory_graph_store_schema(conn)
    if classification != GraphStoreSchemaClassification.CURRENT:
        raise GraphStoreError(f"Schema is not CURRENT: {classification.name}")


def migrate_memory_graph_store_schema(conn: sqlite3.Connection) -> None:
    classification = classify_memory_graph_store_schema(conn)
    if classification == GraphStoreSchemaClassification.CURRENT:
        return
    if classification in (
        GraphStoreSchemaClassification.INCOMPATIBLE,
        GraphStoreSchemaClassification.KNOWN_COMPATIBLE_PARTIAL,
    ):
        raise GraphStoreError(f"Cannot migrate from {classification.name} schema")

    cur = conn.cursor()
    cur.execute("SAVEPOINT migrate_graph_store")
    try:
        cur.execute(_CREATE_META)
        cur.execute(_CREATE_USER_STATE)
        cur.execute(_CREATE_NODES)
        cur.execute(_CREATE_EDGES)
        cur.execute(_CREATE_NODE_SUPPORTS)
        cur.execute(_CREATE_EDGE_SUPPORTS)
        cur.execute(_CREATE_EXCLUSIONS)

        cur.execute(
            "INSERT INTO memory_graph_store_meta (singleton_id, schema_version) VALUES (1, ?)",
            (MEMORY_GRAPH_STORE_SCHEMA_VERSION,),
        )

        if (
            classify_memory_graph_store_schema(conn)
            != GraphStoreSchemaClassification.CURRENT
        ):
            raise GraphStoreError("Schema classification failed after migration")

        cur.execute("RELEASE SAVEPOINT migrate_graph_store")
    except Exception:
        cur.execute("ROLLBACK TO migrate_graph_store")
        cur.execute("RELEASE SAVEPOINT migrate_graph_store")
        raise


def _enforce_foreign_keys(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys")
    row = cur.fetchone()
    if not row or row[0] == 0:
        raise GraphStoreError(
            "PRAGMA foreign_keys=ON is required for memory graph operations"
        )


def clear_user_graph_projection(conn: sqlite3.Connection, user_id: int) -> None:
    validate_memory_graph_store_schema(conn)
    _enforce_foreign_keys(conn)
    cur = conn.cursor()
    cur.execute("SAVEPOINT clear_graph_projection")
    try:
        cur.execute("DELETE FROM memory_graph_user_state WHERE user_id = ?", (user_id,))
        cur.execute("RELEASE SAVEPOINT clear_graph_projection")
    except Exception:
        cur.execute("ROLLBACK TO clear_graph_projection")
        cur.execute("RELEASE SAVEPOINT clear_graph_projection")
        raise


# Only for tests!
_FAILURE_INJECTION_HOOK = None


def publish_graph_projection(
    conn: sqlite3.Connection, projection: GraphProjectionResult
) -> None:
    verify_graph_projection_result(projection)
    validate_memory_graph_store_schema(conn)
    _enforce_foreign_keys(conn)

    if projection.snapshot.schema_version != GRAPH_SCHEMA_VERSION:
        raise GraphStoreError(
            f"Snapshot schema version {projection.snapshot.schema_version} != {GRAPH_SCHEMA_VERSION}"
        )

    cur = conn.cursor()
    cur.execute("SAVEPOINT publish_graph")
    try:
        user_id = projection.user_id

        # Active state committed only at savepoint release.
        cur.execute("DELETE FROM memory_graph_user_state WHERE user_id = ?", (user_id,))

        if _FAILURE_INJECTION_HOOK == "after_delete":
            raise Exception("injected failure after_delete")

        canonical_json = serialize_graph_snapshot(projection.snapshot)
        cur.execute(
            """INSERT INTO memory_graph_user_state
               (user_id, graph_schema_version, projection_version, snapshot_id, projection_id,
                canonical_snapshot_json, input_fact_count, projected_fact_count, excluded_fact_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                projection.snapshot.schema_version,
                projection.projection_version,
                projection.snapshot.snapshot_id,
                projection.projection_id,
                canonical_json,
                projection.input_fact_count,
                projection.projected_fact_count,
                projection.excluded_fact_count,
            ),
        )

        if _FAILURE_INJECTION_HOOK == "after_user_state":
            raise Exception("injected failure after_user_state")

        for node in projection.snapshot.nodes:
            cur.execute(
                """INSERT INTO memory_graph_nodes
                   (user_id, node_id, node_type, properties_json, primary_provenance_fact_id, primary_provenance_revision)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    node.node_id,
                    node.node_type,
                    json.dumps(
                        dict(node.properties),
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    node.provenance.fact_id,
                    node.provenance.revision,
                ),
            )

        if _FAILURE_INJECTION_HOOK == "after_nodes":
            raise Exception("injected failure after_nodes")

        for edge in projection.snapshot.edges:
            cur.execute(
                """INSERT INTO memory_graph_edges
                   (user_id, edge_id, source_node_id, target_node_id, relation_type, properties_json, primary_provenance_fact_id, primary_provenance_revision)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    edge.edge_id,
                    edge.source_node_id,
                    edge.target_node_id,
                    edge.relation_type,
                    json.dumps(
                        dict(edge.properties),
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                    edge.provenance.fact_id,
                    edge.provenance.revision,
                ),
            )

        if _FAILURE_INJECTION_HOOK == "after_edges":
            raise Exception("injected failure after_edges")

        for nid, supports in projection.node_supports.items():
            for supp in supports:
                cur.execute(
                    """INSERT INTO memory_graph_node_supports
                       (user_id, node_id, fact_id, revision)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, nid, supp.fact_id, supp.revision),
                )

        if _FAILURE_INJECTION_HOOK == "after_node_supports":
            raise Exception("injected failure after_node_supports")

        for eid, supports in projection.edge_supports.items():
            for supp in supports:
                cur.execute(
                    """INSERT INTO memory_graph_edge_supports
                       (user_id, edge_id, fact_id, revision)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, eid, supp.fact_id, supp.revision),
                )

        if _FAILURE_INJECTION_HOOK == "after_edge_supports":
            raise Exception("injected failure after_edge_supports")

        for ex in projection.exclusions:
            cur.execute(
                """INSERT INTO memory_graph_exclusions
                   (user_id, fact_id, reason)
                   VALUES (?, ?, ?)""",
                (user_id, str(ex.fact_id), ex.reason),
            )

        if _FAILURE_INJECTION_HOOK == "after_exclusions":
            raise Exception("injected failure after_exclusions")

        if _FAILURE_INJECTION_HOOK == "before_release":
            raise Exception("injected failure before_release")

        cur.execute("RELEASE SAVEPOINT publish_graph")
    except Exception:
        cur.execute("ROLLBACK TO publish_graph")
        cur.execute("RELEASE SAVEPOINT publish_graph")
        raise


def load_graph_projection(
    conn: sqlite3.Connection, user_id: int
) -> GraphProjectionResult | None:
    validate_memory_graph_store_schema(conn)
    cur = conn.cursor()

    cur.execute(
        """SELECT projection_version, snapshot_id, projection_id, canonical_snapshot_json, input_fact_count, projected_fact_count, excluded_fact_count, graph_schema_version
           FROM memory_graph_user_state WHERE user_id = ?""",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return None

    (
        proj_version,
        db_snapshot_id,
        db_projection_id,
        canonical_snapshot_json,
        in_count,
        proj_count,
        exc_count,
        graph_schema_version,
    ) = row

    if graph_schema_version != GRAPH_SCHEMA_VERSION:
        raise GraphStoreError("Tampered graph_schema_version")

    if proj_version != MEMORY_GRAPH_PROJECTION_VERSION:
        raise GraphStoreError("Tampered projection_version")

    cur.execute("SELECT COUNT(*) FROM memory_graph_nodes WHERE user_id = ?", (user_id,))
    node_count = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM memory_graph_edges WHERE user_id = ?", (user_id,))
    edge_count = cur.fetchone()[0]

    try:
        snapshot = deserialize_graph_snapshot(canonical_snapshot_json)
    except Exception as e:
        raise GraphStoreError(f"Corrupted snapshot JSON: {e}")

    # Canonical snapshot JSON byte equality check
    re_serialized = serialize_graph_snapshot(snapshot)
    if re_serialized != canonical_snapshot_json:
        raise GraphStoreError("Noncanonical JSON stored")

    if snapshot.snapshot_id != db_snapshot_id:
        raise GraphStoreError("Corrupted snapshot identity in user state")

    if len(snapshot.nodes) != node_count:
        raise GraphStoreError("Node count mismatch between snapshot and tables")

    if len(snapshot.edges) != edge_count:
        raise GraphStoreError("Edge count mismatch between snapshot and tables")

    cur.execute(
        """SELECT node_id, node_type, properties_json, primary_provenance_fact_id, primary_provenance_revision
           FROM memory_graph_nodes WHERE user_id = ?""",
        (user_id,),
    )
    db_nodes = {nr[0]: nr for nr in cur.fetchall()}

    for sn_node in snapshot.nodes:
        if sn_node.node_id not in db_nodes:
            raise GraphStoreError(f"Node {sn_node.node_id} missing from nodes table")
        nr = db_nodes[sn_node.node_id]
        if sn_node.node_type != nr[1]:
            raise GraphStoreError(f"Node {sn_node.node_id} type mismatch")
        props_json = json.dumps(
            dict(sn_node.properties),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if props_json != nr[2]:
            raise GraphStoreError(f"Node {sn_node.node_id} properties mismatch")
        if sn_node.provenance.fact_id != nr[3] or sn_node.provenance.revision != nr[4]:
            raise GraphStoreError(f"Node {sn_node.node_id} provenance mismatch")

    cur.execute(
        """SELECT edge_id, source_node_id, target_node_id, relation_type, properties_json, primary_provenance_fact_id, primary_provenance_revision
           FROM memory_graph_edges WHERE user_id = ?""",
        (user_id,),
    )
    db_edges = {er[0]: er for er in cur.fetchall()}

    for sn_edge in snapshot.edges:
        if sn_edge.edge_id not in db_edges:
            raise GraphStoreError(f"Edge {sn_edge.edge_id} missing from edges table")
        er = db_edges[sn_edge.edge_id]
        if (
            sn_edge.source_node_id != er[1]
            or sn_edge.target_node_id != er[2]
            or sn_edge.relation_type != er[3]
        ):
            raise GraphStoreError(f"Edge {sn_edge.edge_id} relation mismatch")
        props_json = json.dumps(
            dict(sn_edge.properties),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if props_json != er[4]:
            raise GraphStoreError(f"Edge {sn_edge.edge_id} properties mismatch")
        if sn_edge.provenance.fact_id != er[5] or sn_edge.provenance.revision != er[6]:
            raise GraphStoreError(f"Edge {sn_edge.edge_id} provenance mismatch")

    node_supports_dict = {}
    cur.execute(
        "SELECT node_id, fact_id, revision FROM memory_graph_node_supports WHERE user_id = ?",
        (user_id,),
    )
    for nid, fid, rev in cur.fetchall():
        if nid not in node_supports_dict:
            node_supports_dict[nid] = []
        node_supports_dict[nid].append(
            GraphProvenance("sqlite_memory_os_facts", fid, rev)
        )

    edge_supports_dict = {}
    cur.execute(
        "SELECT edge_id, fact_id, revision FROM memory_graph_edge_supports WHERE user_id = ?",
        (user_id,),
    )
    for eid, fid, rev in cur.fetchall():
        if eid not in edge_supports_dict:
            edge_supports_dict[eid] = []
        edge_supports_dict[eid].append(
            GraphProvenance("sqlite_memory_os_facts", fid, rev)
        )

    # verify exact normalized sizes for supports
    if (
        sum(len(v) for v in node_supports_dict.values())
        != cur.execute(
            "SELECT COUNT(*) FROM memory_graph_node_supports WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    ):
        raise GraphStoreError("Node support count mismatch")
    if (
        sum(len(v) for v in edge_supports_dict.values())
        != cur.execute(
            "SELECT COUNT(*) FROM memory_graph_edge_supports WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    ):
        raise GraphStoreError("Edge support count mismatch")

    for sn_node in snapshot.nodes:
        if sn_node.node_id not in node_supports_dict:
            raise GraphStoreError(f"Node {sn_node.node_id} missing supports")

    for sn_edge in snapshot.edges:
        if sn_edge.edge_id not in edge_supports_dict:
            raise GraphStoreError(f"Edge {sn_edge.edge_id} missing supports")

    cur.execute(
        "SELECT fact_id, reason FROM memory_graph_exclusions WHERE user_id = ?",
        (user_id,),
    )
    excl = [ProjectionExclusion(int(er[0]), er[1]) for er in cur.fetchall()]

    if len(excl) != exc_count:
        raise GraphStoreError("Tampered excluded_fact_count")

    try:
        result = GraphProjectionResult(
            projection_version=proj_version,
            user_id=user_id,
            snapshot=snapshot,
            projection_id=db_projection_id,
            input_fact_count=in_count,
            projected_fact_count=proj_count,
            excluded_fact_count=exc_count,
            node_supports=MappingProxyType({
                k: tuple(sorted(v, key=lambda x: (int(x.fact_id), x.revision)))
                for k, v in node_supports_dict.items()
            }),
            edge_supports=MappingProxyType({
                k: tuple(sorted(v, key=lambda x: (int(x.fact_id), x.revision)))
                for k, v in edge_supports_dict.items()
            }),
            exclusions=tuple(sorted(excl, key=lambda x: x.fact_id)),
        )
        verify_graph_projection_result(result)
    except Exception as e:
        raise GraphStoreError(f"Validation of reconstructed projection failed: {e}")

    return result


def rebuild_user_graph_store(conn: sqlite3.Connection, user_id: int) -> None:
    validate_memory_graph_store_schema(conn)
    facts = read_authoritative_memory_facts(conn, user_id=user_id)
    projection = project_authoritative_memory_facts(facts, user_id=user_id)
    publish_graph_projection(conn, projection)
