import pytest
from pathlib import Path
import json
from ai_engineering.memory_graph_eval import (
    run_eval_engine, MemoryGraphEvalReport,
    _parse_json_strict, _compute_corpus_digest
)

def test_eval_engine_runs_successfully():
    corpus_dir = Path("evals/memory_graph")
    report = run_eval_engine(corpus_dir)
    assert report.fail_count == 0
    assert report.critical_fail_count == 0
    assert report.corpus_digest != ""
    assert report.report_id != ""

def test_parse_json_strict_rejects_duplicates():
    with pytest.raises(ValueError, match="Duplicate key"):
        _parse_json_strict('{"a": 1, "a": 2}')

def test_parse_json_strict_rejects_nan():
    with pytest.raises(ValueError, match="not allowed"):
        _parse_json_strict('{"a": NaN}')

def test_parse_json_strict_rejects_infinity():
    with pytest.raises(ValueError, match="not allowed"):
        _parse_json_strict('{"a": Infinity}')

def test_compute_corpus_digest_missing_manifest(tmp_path):
    with pytest.raises(ValueError, match="manifest.json not found"):
        _compute_corpus_digest(tmp_path)

def test_compute_corpus_digest_invalid_manifest(tmp_path):
    (tmp_path / "manifest.json").write_text('{"schema_version": 2}')
    with pytest.raises(ValueError, match="missing or unknown fields"):
        _compute_corpus_digest(tmp_path)

def test_compute_corpus_digest_missing_dataset(tmp_path):
    manifest = {
      "schema_version": 1,
      "engine_version": 1,
      "dataset_version": "memory-graph-v1",
      "corpus_status": "CANDIDATE",
      "datasets": [
          {"category": "RETRIEVAL", "path": "datasets/retrieval.jsonl", "critical": True},
          {"category": "FRESHNESS", "path": "datasets/freshness.jsonl", "critical": True},
          {"category": "PRIVACY", "path": "datasets/privacy.jsonl", "critical": True},
          {"category": "ISOLATION", "path": "datasets/isolation.jsonl", "critical": True},
          {"category": "INTEGRITY", "path": "datasets/integrity.jsonl", "critical": True},
          {"category": "CONVERGENCE", "path": "datasets/convergence.jsonl", "critical": True},
          {"category": "DETERMINISM", "path": "datasets/determinism.jsonl", "critical": True},
          {"category": "TRANSACTION", "path": "datasets/transaction.jsonl", "critical": True},
      ]
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "datasets").mkdir()

    with pytest.raises(ValueError, match="File missing"):
        _compute_corpus_digest(tmp_path)

def test_report_id_semantic_binding():
    import dataclasses
    from ai_engineering.memory_graph_eval import run_eval_engine
    import hashlib
    import json
    
    corpus_dir = Path("evals/memory_graph")
    r1 = run_eval_engine(corpus_dir)
    r2 = run_eval_engine(corpus_dir)
    assert r1.report_id == r2.report_id

    assert len(r1.report_id) == 64
    assert all(c in "0123456789abcdef" for c in r1.report_id)

    def compute_id(report):
        d = dataclasses.asdict(report)
        d["report_id"] = ""
        payload = json.dumps(d, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()
        
    assert compute_id(r1) == r1.report_id
    
    r3 = dataclasses.replace(r1, fail_count=r1.fail_count + 1)
    assert compute_id(r3) != r1.report_id

def test_report_deterministic():
    corpus_dir = Path("evals/memory_graph")
    r1 = run_eval_engine(corpus_dir)
    r2 = run_eval_engine(corpus_dir)
    import dataclasses
    assert dataclasses.asdict(r1) == dataclasses.asdict(r2)

def test_CLI_exit_codes(tmp_path, monkeypatch):
    import subprocess
    import os
    import json
    
    # Run CLI on valid passing corpus (default evals/memory_graph)
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    res = subprocess.run(["python", "scripts/run_memory_graph_evals.py"], capture_output=True, text=True, env=env)
    assert res.returncode == 0
    assert "VERDICT=PASS" in res.stdout

    # Create invalid corpus for BLOCKED
    invalid_dir = tmp_path / "invalid"
    invalid_dir.mkdir()
    (invalid_dir / "manifest.json").write_text('{"schema_version": 2}')
    res2 = subprocess.run(["python", "scripts/run_memory_graph_evals.py", "--root", str(invalid_dir)], capture_output=True, text=True, env=env)
    assert res2.returncode == 2
    assert "STATUS=BLOCKED" in res2.stdout
    assert "Traceback" not in res2.stdout
    assert "Traceback" not in res2.stderr

    # Create valid corpus with failure for FAIL
    fail_dir = tmp_path / "fail"
    fail_dir.mkdir()
    fail_dir.joinpath("datasets").mkdir()
    (fail_dir / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "engine_version": 1, "dataset_version": "memory-graph-v1", "corpus_status": "CANDIDATE",
        "datasets": [
            {"category": "RETRIEVAL", "path": "datasets/r.jsonl", "critical": True},
            {"category": "FRESHNESS", "path": "datasets/f.jsonl", "critical": True},
            {"category": "PRIVACY", "path": "datasets/p.jsonl", "critical": True},
            {"category": "ISOLATION", "path": "datasets/i.jsonl", "critical": True},
            {"category": "INTEGRITY", "path": "datasets/in.jsonl", "critical": True},
            {"category": "CONVERGENCE", "path": "datasets/c.jsonl", "critical": True},
            {"category": "DETERMINISM", "path": "datasets/d.jsonl", "critical": True},
            {"category": "TRANSACTION", "path": "datasets/t.jsonl", "critical": True}
        ]
    }))
    for n in ["f", "p", "i", "in", "c", "d", "t"]:
        (fail_dir / "datasets" / f"{n}.jsonl").write_text("")
        
    (fail_dir / "datasets" / "r.jsonl").write_text(json.dumps({
        "schema_version": 1, "scenario_id": "r1", "category": "RETRIEVAL", "critical": True,
        "setup": {"users": []}, "action": {"type": "READ_CONTEXT", "user_id": 1, "query": {"entity": "e", "key": "k"}},
        "expected": {"read_status": "MATCH", "match_count": 1}
    }) + "\n")

    res3 = subprocess.run(["python", "scripts/run_memory_graph_evals.py", "--root", str(fail_dir)], capture_output=True, text=True, env=env)
    assert res3.returncode == 1
    assert "VERDICT=FAIL" in res3.stdout
    assert "Traceback" not in res3.stdout
    assert "Traceback" not in res3.stderr


def test_safety_counters(tmp_path, monkeypatch):
    import json
    import ai_engineering.memory_graph_eval as eval_mod

    (tmp_path / 'manifest.json').write_text(json.dumps({
        'schema_version': 1, 'engine_version': 1, 'dataset_version': 'memory-graph-v1',
        'corpus_status': 'CANDIDATE', 'datasets': [
            {'category': cat, 'path': f'{cat}.jsonl', 'critical': True}
            for cat in ['RETRIEVAL', 'FRESHNESS', 'PRIVACY', 'ISOLATION', 'INTEGRITY', 'CONVERGENCE', 'DETERMINISM', 'TRANSACTION']
        ]
    }))

    for cat in ['RETRIEVAL', 'FRESHNESS', 'PRIVACY', 'ISOLATION', 'INTEGRITY', 'CONVERGENCE', 'DETERMINISM', 'TRANSACTION']:
        (tmp_path / f'{cat}.jsonl').write_text(json.dumps({
            'schema_version': 1,
            'scenario_id': f'{cat}1',
            'category': cat,
            'critical': True,
            'setup': {'users': []},
            'action': {'type': 'QUERY', 'user_id': 1},
            'expected': {},
            'steps': []
        }) + '\n')

    def mock_eval(scenario):
        kwargs = {
            'scenario_id': scenario.scenario_id,
            'category': scenario.category,
            'status': 'PASS',
            'reason_code': 'PASS',
            'critical': True,
            'false_ready': False,
            'cross_user_leakage': False,
            'excluded_fact_leakage': False,
            'integrity_fail_open': False,
            'unexpected_db_mutation': False,
            'nondeterministic_result': False,
        }
        if scenario.category == 'PRIVACY':
            kwargs['cross_user_leakage'] = True
            kwargs['status'] = 'FAIL'
        elif scenario.category == 'ISOLATION':
            kwargs['excluded_fact_leakage'] = True
            kwargs['status'] = 'FAIL'
        elif scenario.category == 'INTEGRITY':
            kwargs['integrity_fail_open'] = True
            kwargs['status'] = 'FAIL'
        elif scenario.category == 'FRESHNESS':
            kwargs['unexpected_db_mutation'] = True
            kwargs['status'] = 'FAIL'
        elif scenario.category == 'DETERMINISM':
            kwargs['nondeterministic_result'] = True
            kwargs['status'] = 'FAIL'
        elif scenario.category == 'TRANSACTION':
            kwargs['false_ready'] = True
            kwargs['status'] = 'FAIL'
        return eval_mod.MemoryGraphScenarioResult(**kwargs)

    monkeypatch.setattr(eval_mod, 'evaluate_scenario', mock_eval)

    report = eval_mod.run_eval_engine(tmp_path)
    
    assert report.cross_user_leakage_count == 1
    assert report.excluded_fact_leakage_count == 1
    assert report.integrity_fail_open_count == 1
    assert report.unexpected_db_mutation_count == 1
    assert report.nondeterministic_result_count == 1
    assert report.false_ready_count == 1

