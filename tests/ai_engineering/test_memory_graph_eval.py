import pytest
from pathlib import Path
from ai_engineering.memory_graph_eval import run_eval_engine, MemoryGraphEvalReport

def test_eval_engine():
    # Will fail if the corpus is malformed or tests fail.
    # We just run the engine on the generated datasets for now
    corpus_dir = Path("evals/memory_graph")
    report = run_eval_engine(corpus_dir)
    assert report.fail_count == 0
    assert report.false_ready_count == 0
    assert report.cross_user_leakage_count == 0
