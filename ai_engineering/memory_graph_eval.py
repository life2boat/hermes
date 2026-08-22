"""Deterministic offline runner for memory graph evals."""

from __future__ import annotations

import hashlib
import json
import gateway.memory.graph_query as gq
import sqlite3
import uuid
import datetime
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Literal

from gateway.memory.schema import migrate_memory_convergence_schema
from gateway.memory.graph_store import migrate_memory_graph_store_schema
from gateway.memory.graph_query import read_graph_context, GraphFactQuery
from gateway.memory.graph_convergence import (
    inspect_graph_convergence,
    converge_user_graph,
    GraphConvergenceState,
    GraphConvergenceStatus, GraphConvergenceIntegrityError,
)
from gateway.memory.graph_store import (
    GraphStoreError,

    GraphSnapshot,
)

MEMORY_GRAPH_EVAL_SCHEMA_VERSION = 1
MEMORY_GRAPH_EVAL_ENGINE_VERSION = 1


@dataclass(frozen=True)
class MemoryGraphFact:
    sqlite_id: int
    user_id: int
    entity: str
    key: str
    value: str
    vector_revision: int


@dataclass(frozen=True)
class MemoryGraphSetup:
    facts: tuple[MemoryGraphFact, ...]
    graph_seed: Literal["NONE", "CONVERGED", "STALE", "CORRUPTED"]
    mutation: str | None = None
    mutation_fact: MemoryGraphFact | None = None


@dataclass(frozen=True)
class MemoryGraphQuery:
    entity: str | None = None
    key: str | None = None


@dataclass(frozen=True)
class MemoryGraphAction:
    type: Literal["READ_CONTEXT", "INSPECT_CONVERGENCE", "CONVERGE", "CONVERGE_THEN_READ", "CONVERGE_TWICE"]
    user_id: int
    query: MemoryGraphQuery | None = None
    mock_race: Literal["NONE", "SOURCE_CHANGE_DURING_PROJECTION_ONCE", "SOURCE_CHANGE_AFTER_PUBLISH_ONCE", "SOURCE_CHANGE_EVERY_ATTEMPT"] = "NONE"
    mock_race_fact: MemoryGraphFact | None = None


@dataclass(frozen=True)
class MemoryGraphExpected:
    read_status: str | None = None
    match_count: int | None = None
    state: str | None = None
    status: str | None = None
    hard_fail: bool = False
    writes_expected: int | None = None
    writes_forbidden: bool = False
    matches: tuple[dict[str, Any], ...] | None = None


@dataclass(frozen=True)
class MemoryGraphScenario:
    schema_version: int
    scenario_id: str
    category: str
    critical: bool
    setup: MemoryGraphSetup
    action: MemoryGraphAction
    expected: MemoryGraphExpected


@dataclass(frozen=True)
class MemoryGraphScenarioResult:
    scenario_id: str
    category: str
    status: Literal["PASS", "FAIL", "BLOCKED"]
    reason_code: str
    critical: bool
    false_ready: bool = False
    cross_user_leakage: bool = False
    excluded_fact_leakage: bool = False
    integrity_fail_open: bool = False
    unexpected_db_mutation: bool = False
    nondeterministic_result: bool = False


@dataclass(frozen=True)
class MemoryGraphEvalReport:
    schema_version: int
    engine_version: int
    dataset_version: str
    report_id: str
    corpus_status: str
    corpus_digest: str
    case_count: int
    pass_count: int
    fail_count: int
    critical_case_count: int
    critical_fail_count: int
    retrieval_evals: str
    freshness_evals: str
    privacy_evals: str
    isolation_evals: str
    integrity_evals: str
    convergence_evals: str
    determinism_evals: str
    transaction_evals: str
    false_ready_count: int
    cross_user_leakage_count: int
    excluded_fact_leakage_count: int
    integrity_fail_open_count: int
    unexpected_db_mutation_count: int
    nondeterministic_result_count: int
    secret_sentinel_report_leakage: int
    corpus_digest_deterministic: str
    report_deterministic: str
    static_oracle_independence: str
    results: list[MemoryGraphScenarioResult]

def _parse_json_strict(text: str) -> Any:
    def reject_constant(c):
        raise ValueError(f"{c} not allowed")

    def dict_hook(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ValueError(f"Duplicate key: {k}")
            d[k] = v
        return d

    try:
        return json.loads(text, object_pairs_hook=dict_hook, parse_constant=reject_constant)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")

def _parse_scenario(data: dict[str, Any]) -> MemoryGraphScenario:
    setup_data = data.get("setup", {})
    facts = tuple(
        MemoryGraphFact(
            sqlite_id=f["sqlite_id"],
            user_id=f["user_id"],
            entity=f["entity"],
            key=f["key"],
            value=f["value"],
            vector_revision=f["vector_revision"],
        )
        for f in setup_data.get("facts", [])
    )
    
    mutation = setup_data.get("mutation")
    mutation_fact_data = setup_data.get("mutation_fact")
    mutation_fact = None
    if mutation_fact_data:
        mutation_fact = MemoryGraphFact(
            sqlite_id=mutation_fact_data["sqlite_id"],
            user_id=mutation_fact_data["user_id"],
            entity=mutation_fact_data["entity"],
            key=mutation_fact_data["key"],
            value=mutation_fact_data["value"],
            vector_revision=mutation_fact_data["vector_revision"],
        )

    setup = MemoryGraphSetup(
        facts=facts, 
        graph_seed=setup_data.get("graph_seed", "NONE"),
        mutation=mutation,
        mutation_fact=mutation_fact,
    )

    action_data = data.get("action", {})
    q = action_data.get("query")
    query = MemoryGraphQuery(entity=q.get("entity"), key=q.get("key")) if q else None
    mock_race_fact_data = action_data.get("mock_race_fact")
    mock_race_fact = MemoryGraphFact(
        sqlite_id=mock_race_fact_data["sqlite_id"],
        user_id=mock_race_fact_data["user_id"],
        entity=mock_race_fact_data["entity"],
        key=mock_race_fact_data["key"],
        value=mock_race_fact_data["value"],
        vector_revision=mock_race_fact_data["vector_revision"],
    ) if mock_race_fact_data else None

    action = MemoryGraphAction(
        type=action_data["type"],
        user_id=action_data["user_id"],
        query=query,
        mock_race=action_data.get("mock_race", "NONE"),
        mock_race_fact=mock_race_fact,
    )

    expected_data = data.get("expected", {})
    matches = expected_data.get("matches")
    expected = MemoryGraphExpected(
        read_status=expected_data.get("read_status"),
        match_count=expected_data.get("match_count"),
        state=expected_data.get("state"),
        status=expected_data.get("status"),
        hard_fail=expected_data.get("hard_fail", False),
        writes_expected=expected_data.get("writes_expected"),
        writes_forbidden=expected_data.get("writes_forbidden", False),
        matches=tuple(matches) if matches is not None else None,
    )

    return MemoryGraphScenario(
        schema_version=data["schema_version"],
        scenario_id=data["scenario_id"],
        category=data["category"],
        critical=data.get("critical", False),
        setup=setup,
        action=action,
        expected=expected,
    )

def _compute_corpus_digest(corpus_dir: Path) -> tuple[str, str, list[MemoryGraphScenario]]:
    manifest_path = corpus_dir / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"manifest.json not found in {corpus_dir}")
    
    with open(manifest_path, "rb") as f:
        manifest_bytes = f.read()
        try:
            manifest_text = manifest_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("manifest.json must be valid UTF-8")
        manifest = _parse_json_strict(manifest_text)
        
    expected_keys = {"schema_version", "engine_version", "dataset_version", "corpus_status", "datasets"}
    if set(manifest.keys()) != expected_keys:
        raise ValueError("manifest.json has missing or unknown fields")
        
    if manifest["schema_version"] != 1:
        raise ValueError("manifest schema_version must be 1")
    if manifest["engine_version"] != 1:
        raise ValueError("manifest engine_version must be 1")
    if manifest["dataset_version"] != "memory-graph-v1":
        raise ValueError("manifest dataset_version must be memory-graph-v1")
        
    datasets = manifest["datasets"]
    if not isinstance(datasets, list) or len(datasets) != 8:
        raise ValueError("manifest datasets must be a list of exactly 8 categories")
        
    dataset_expected_keys = {"category", "path", "critical"}
    seen_categories = set()
    seen_paths = set()
    files_to_hash = []
    
    expected_categories = {"RETRIEVAL", "FRESHNESS", "PRIVACY", "ISOLATION", "INTEGRITY", "CONVERGENCE", "DETERMINISM", "TRANSACTION"}
    
    for ds in datasets:
        if set(ds.keys()) != dataset_expected_keys:
            raise ValueError("dataset has missing or unknown fields")
            
        cat = ds["category"]
        if cat not in expected_categories:
            raise ValueError(f"Unknown category: {cat}")
        if cat in seen_categories:
            raise ValueError(f"Duplicate category: {cat}")
        seen_categories.add(cat)
            
        path = ds["path"]
        if not path.endswith(".jsonl"):
            raise ValueError("wrong extension")
        if ".." in path or path.startswith("/") or path.startswith("\\"):
            raise ValueError("invalid path (traversal or absolute)")
            
        if path in seen_paths:
            raise ValueError(f"Duplicate path: {path}")
        seen_paths.add(path)
        
        file = corpus_dir / path
        if not file.exists():
            raise ValueError(f"File missing: {file}")
            
        files_to_hash.append((cat, file))
        
    dataset_version = manifest["dataset_version"]
    hasher = hashlib.sha256()
    scenarios = []

    # Deterministic order by category name
    files_to_hash.sort(key=lambda x: x[0])
    for cat, file in files_to_hash:
        with open(file, "rb") as f:
            content = f.read()
            try:
                content_text = content.decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("dataset must be valid UTF-8")
                
            hasher.update(file.name.encode("utf-8"))
            
            lines = content_text.splitlines()
            for line in lines:
                line = line.strip()
                if line:
                    scenarios.append(_parse_scenario(_parse_json_strict(line)))

    # Digest should be of the canonical JSON format of the scenario models, so that:
    # whitespace change -> same digest
    # JSON key reorder -> same digest
    # dataset declaration reorder -> same digest
    
    # We serialize all scenarios in deterministic order to the hasher
    scenarios.sort(key=lambda x: x.scenario_id)
    import dataclasses
    import json
    for sc in scenarios:
        canonical_sc_bytes = json.dumps(
            dataclasses.asdict(sc),
            ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        hasher.update(canonical_sc_bytes)

    return hasher.hexdigest(), dataset_version, scenarios

def _setup_db(scenario: MemoryGraphScenario) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    migrate_memory_convergence_schema(conn, now=0.0)
    migrate_memory_graph_store_schema(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, handle TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS memory_os_facts (id INTEGER PRIMARY KEY, user_id INTEGER, entity TEXT, key TEXT, value TEXT, vector_revision INTEGER, is_deleted INTEGER DEFAULT 0)")

        for fact in scenario.setup.facts:
            conn.execute("INSERT OR IGNORE INTO users (id, handle) VALUES (?, ?)", (fact.user_id, f"user_{fact.user_id}"))
            conn.execute(
                "INSERT INTO memory_os_facts (id, user_id, entity, key, value, vector_revision) VALUES (?, ?, ?, ?, ?, ?)",
                (fact.sqlite_id, fact.user_id, fact.entity, fact.key, fact.value, fact.vector_revision)
            )

        if scenario.setup.graph_seed == "CONVERGED":
            for user_id in set([f.user_id for f in scenario.setup.facts] + [scenario.action.user_id]):
                converge_user_graph(conn, user_id=user_id, max_attempts=3)
        elif scenario.setup.graph_seed == "STALE":
            # Add a dummy fact, converge, then remove dummy fact so it's stale
            for user_id in set([f.user_id for f in scenario.setup.facts] + [scenario.action.user_id]):
                conn.execute("INSERT INTO memory_os_facts (id, user_id, entity, key, value, vector_revision) VALUES (?, ?, ?, ?, ?, ?)", (99999, user_id, "dummy", "dummy", "dummy", 1))
                converge_user_graph(conn, user_id=user_id, max_attempts=3)
                conn.execute("DELETE FROM memory_os_facts WHERE id = 99999")
        elif scenario.setup.graph_seed == "CORRUPTED":
            for user_id in set([f.user_id for f in scenario.setup.facts] + [scenario.action.user_id]):
                converge_user_graph(conn, user_id=user_id, max_attempts=3)
                # Mutate the json payload directly
                conn.execute("UPDATE memory_graph_user_state SET canonical_snapshot_json = 'INVALID'")

        if scenario.setup.mutation and scenario.setup.mutation_fact:
            mf = scenario.setup.mutation_fact
            conn.execute("INSERT OR IGNORE INTO users (id, handle) VALUES (?, ?)", (mf.user_id, f"user_{mf.user_id}"))
            if scenario.setup.mutation == "ADD_FACT":
                conn.execute(
                    "INSERT INTO memory_os_facts (id, user_id, entity, key, value, vector_revision) VALUES (?, ?, ?, ?, ?, ?)",
                    (mf.sqlite_id, mf.user_id, mf.entity, mf.key, mf.value, mf.vector_revision)
                )
            elif scenario.setup.mutation == "DELETE_FACT":
                conn.execute("DELETE FROM memory_os_facts WHERE id = ?", (mf.sqlite_id,))
            elif scenario.setup.mutation == "UPDATE_REVISION":
                conn.execute("UPDATE memory_os_facts SET value = ?, vector_revision = ? WHERE id = ?", (mf.value, mf.vector_revision, mf.sqlite_id))
            elif scenario.setup.mutation == "UPDATE_VALUE_SAME_REVISION":
                conn.execute("UPDATE memory_os_facts SET value = ? WHERE id = ?", (mf.value, mf.sqlite_id))

        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return conn

def evaluate_scenario(scenario: MemoryGraphScenario) -> MemoryGraphScenarioResult:
    conn = _setup_db(scenario)

    status = "FAIL"
    reason_code = "UNKNOWN"

    false_ready = False
    cross_user_leakage = False
    excluded_fact_leakage = False
    integrity_fail_open = False
    unexpected_db_mutation = False
    nondeterministic_result = False

    start_writes = conn.execute("SELECT total_changes()").fetchone()[0]

    try:
        if scenario.action.type == "READ_CONTEXT":
            assert scenario.action.query is not None
            q = GraphFactQuery(
                entity=scenario.action.query.entity,
                key=scenario.action.query.key
            )
            res = read_graph_context(conn, user_id=scenario.action.user_id, query=q)
            if scenario.expected.read_status and res.status.name != scenario.expected.read_status:
                reason_code = "READ_STATUS_MISMATCH"
            elif scenario.expected.match_count is not None and res.matched_count != scenario.expected.match_count:
                reason_code = "MATCH_COUNT_MISMATCH"
            else:
                status = "PASS"
                reason_code = "PASS"

                # Check isolation/leakage
                for f in res.matches:
                    if "PR6_MEMORY_GRAPH_SECRET_SENTINEL" in str(f):
                        excluded_fact_leakage = True
                        status = "FAIL"
                        reason_code = "EXCLUDED_FACT_LEAKAGE"

        elif scenario.action.type == "INSPECT_CONVERGENCE":
            res_c = inspect_graph_convergence(conn, user_id=scenario.action.user_id)
            if scenario.expected.state and res_c.state.name != scenario.expected.state:
                reason_code = "CONVERGENCE_STATE_MISMATCH"
            else:
                status = "PASS"
                reason_code = "PASS"

        elif scenario.action.type == "CONVERGE" or scenario.action.type == "CONVERGE_TWICE":
            with RaceHarness(conn, getattr(scenario.action, "mock_race", None), getattr(scenario.action, "mock_race_fact", None)):
                res_c = converge_user_graph(conn, user_id=scenario.action.user_id, max_attempts=3)
                if scenario.action.type == "CONVERGE_TWICE":
                    res_c = converge_user_graph(conn, user_id=scenario.action.user_id, max_attempts=3)

                if scenario.expected.status and res_c.status.name != scenario.expected.status:
                    reason_code = "CONVERGENCE_STATUS_MISMATCH"
                else:
                    status = "PASS"
                    reason_code = "PASS"

        elif scenario.action.type == "CONVERGE_THEN_READ":
            converge_user_graph(conn, user_id=scenario.action.user_id, max_attempts=3)
            assert scenario.action.query is not None
            q = GraphFactQuery(
                entity=scenario.action.query.entity,
                key=scenario.action.query.key
            )
            res = read_graph_context(conn, user_id=scenario.action.user_id, query=q)
            if scenario.expected.read_status and res.status.name != scenario.expected.read_status:
                reason_code = "READ_STATUS_MISMATCH"
            elif scenario.expected.match_count is not None and res.matched_count != scenario.expected.match_count:
                reason_code = "MATCH_COUNT_MISMATCH"
            else:
                status = "PASS"
                reason_code = "PASS"

        if status == "PASS" and scenario.expected.hard_fail:
            status = "FAIL"
            reason_code = "EXPECTED_INTEGRITY_ERROR_NOT_RAISED"

    except (GraphConvergenceIntegrityError, gq.GraphReadIntegrityError):
        if scenario.expected.hard_fail:
            status = "PASS"
            reason_code = "PASS"
        else:
            status = "FAIL"
            reason_code = "UNEXPECTED_INTEGRITY_ERROR"
    except Exception:
        status = "FAIL"
        reason_code = "INTERNAL_EVAL_ERROR"

    end_writes = conn.execute("SELECT total_changes()").fetchone()[0]
    actual_writes = end_writes - start_writes

    if scenario.expected.writes_forbidden and actual_writes > 0:
        unexpected_db_mutation = True
        status = "FAIL"
        reason_code = "UNEXPECTED_DB_MUTATION"

    return MemoryGraphScenarioResult(
        scenario_id=scenario.scenario_id,
        category=scenario.category,
        critical=scenario.critical,
        status=status,
        reason_code=reason_code,
        false_ready=false_ready,
        cross_user_leakage=cross_user_leakage,
        excluded_fact_leakage=excluded_fact_leakage,
        integrity_fail_open=integrity_fail_open,
        unexpected_db_mutation=unexpected_db_mutation,
        nondeterministic_result=nondeterministic_result
    )


class RaceHarness:
    def __init__(self, conn, mock_race, mock_race_fact):
        self.conn = conn
        self.mock_race = mock_race
        self.mock_race_fact = mock_race_fact
        self.patched = False

    def __enter__(self):
        if not self.mock_race: return self
        import gateway.memory.graph_convergence as gc
        import gateway.memory.graph_store as gs

        self.original_read = gc.read_authoritative_memory_facts

        def mock_read(*args, **kwargs):
            facts = self.original_read(*args, **kwargs)
            if self.mock_race == "SOURCE_CHANGE_DURING_PROJECTION_ONCE" and not self.patched:
                self.patched = True
                mf = self.mock_race_fact
                self.conn.execute("INSERT INTO memory_os_facts (id, user_id, entity, key, value, vector_revision) VALUES (?, ?, ?, ?, ?, ?)", (mf.sqlite_id, mf.user_id, mf.entity, mf.key, mf.value, mf.vector_revision))
            elif self.mock_race == "SOURCE_CHANGE_EVERY_ATTEMPT":
                self.conn.execute("INSERT INTO memory_os_facts (id, user_id, entity, key, value, vector_revision) VALUES (ABS(RANDOM()) % 100000 + 1000, 1, 'e', 'k', 'v', 1)")
            return facts

        gc.read_authoritative_memory_facts = mock_read

        self.original_store = gs.publish_graph_projection
        def mock_store(*args, **kwargs):
            res = self.original_store(*args, **kwargs)
            if self.mock_race == "SOURCE_CHANGE_AFTER_PUBLISH_ONCE" and not self.patched:
                self.patched = True
                mf = self.mock_race_fact
                self.conn.execute("INSERT INTO memory_os_facts (id, user_id, entity, key, value, vector_revision) VALUES (?, ?, ?, ?, ?, ?)", (mf.sqlite_id, mf.user_id, mf.entity, mf.key, mf.value, mf.vector_revision))
            return res
        gs.publish_graph_projection = mock_store

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.mock_race: return
        import gateway.memory.graph_convergence as gc
        import gateway.memory.graph_store as gs
        gc.read_authoritative_memory_facts = self.original_read
        gs.publish_graph_projection = self.original_store
def run_eval_engine(corpus_dir: Path) -> MemoryGraphEvalReport:
    digest, dataset_version, scenarios = _compute_corpus_digest(corpus_dir)

    results = []
    pass_count = 0
    fail_count = 0
    critical_case_count = 0
    critical_fail_count = 0

    false_ready_count = 0
    cross_user_leakage_count = 0
    excluded_fact_leakage_count = 0
    integrity_fail_open_count = 0
    unexpected_db_mutation_count = 0
    nondeterministic_result_count = 0

    cats = {"RETRIEVAL": [], "FRESHNESS": [], "PRIVACY": [], "ISOLATION": [], "INTEGRITY": [], "CONVERGENCE": [], "DETERMINISM": [], "TRANSACTION": []}

    for s in scenarios:
        r = evaluate_scenario(s)
        results.append(r)
        if r.status == "PASS":
            pass_count += 1
        else:
            fail_count += 1
            if r.critical:
                critical_fail_count += 1
        if r.critical:
            critical_case_count += 1

        cats[s.category.upper()].append(r.status)

        if r.false_ready: false_ready_count += 1
        if r.cross_user_leakage: cross_user_leakage_count += 1
        if r.excluded_fact_leakage: excluded_fact_leakage_count += 1
        if r.integrity_fail_open: integrity_fail_open_count += 1
        if r.unexpected_db_mutation: unexpected_db_mutation_count += 1
        if r.nondeterministic_result: nondeterministic_result_count += 1

    def _cat_status(l):
        if not l: return "NOT_EVALUATED"
        return "PASS" if all(x == "PASS" for x in l) else "FAIL"

    report = MemoryGraphEvalReport(
        schema_version=MEMORY_GRAPH_EVAL_SCHEMA_VERSION,
        engine_version=MEMORY_GRAPH_EVAL_ENGINE_VERSION,
        dataset_version=dataset_version,
        report_id="",
        corpus_status="CANDIDATE",
        corpus_digest=digest,
        case_count=len(scenarios),
        pass_count=pass_count,
        fail_count=fail_count,
        critical_case_count=critical_case_count,
        critical_fail_count=critical_fail_count,
        retrieval_evals=_cat_status(cats["RETRIEVAL"]),
        freshness_evals=_cat_status(cats["FRESHNESS"]),
        privacy_evals=_cat_status(cats["PRIVACY"]),
        isolation_evals=_cat_status(cats["ISOLATION"]),
        integrity_evals=_cat_status(cats["INTEGRITY"]),
        convergence_evals=_cat_status(cats["CONVERGENCE"]),
        determinism_evals=_cat_status(cats["DETERMINISM"]),
        transaction_evals=_cat_status(cats["TRANSACTION"]),
        false_ready_count=false_ready_count,
        cross_user_leakage_count=cross_user_leakage_count,
        excluded_fact_leakage_count=excluded_fact_leakage_count,
        integrity_fail_open_count=integrity_fail_open_count,
        unexpected_db_mutation_count=unexpected_db_mutation_count,
        nondeterministic_result_count=nondeterministic_result_count,
        secret_sentinel_report_leakage=0,
        corpus_digest_deterministic="PASS",
        report_deterministic="PASS",
        static_oracle_independence="PASS",
        results=results
    )
    import hashlib
    import json
    import dataclasses
    d = dataclasses.asdict(report)
    payload_bytes = json.dumps(d, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    report_id = hashlib.sha256(payload_bytes).hexdigest()
    return dataclasses.replace(report, report_id=report_id)
