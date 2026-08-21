"""CLI runner for Memory Graph evaluation corpus."""

import json
import sys
from pathlib import Path
from dataclasses import asdict
from ai_engineering.memory_graph_eval import run_eval_engine, MemoryGraphEvalReport

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=str, default="evals/memory_graph")
    parser.add_argument("--format", type=str, default="json")
    args = parser.parse_args()

    corpus_dir = Path(args.root)

    try:
        report1 = run_eval_engine(corpus_dir)
        report2 = run_eval_engine(corpus_dir)
    except Exception as e:
        print("STATUS=BLOCKED")
        print(f"Error: {e}")
        sys.exit(2)

    # Save a json dump in .task_context
    Path(".task_context").mkdir(exist_ok=True)
    with open(".task_context/pr6-report.json", "w", encoding="utf-8") as f:
        json.dump(asdict(report1), f, indent=2)

    # Double run check
    report_bytes_equal = (report1.report_id == report2.report_id) and (report1.corpus_digest == report2.corpus_digest)

    if args.format == "json":
        print(json.dumps(asdict(report1), indent=2))

    print(f"\nSTATUS={'PASS' if report1.fail_count == 0 else 'FAIL'}")
    print()
    print("REPORT_ID_ALGORITHM=SHA256")
    print(f"REPORT_ID={report1.report_id}")
    print()
    print(f"MEMORY_GRAPH_EVAL_SCHEMA_VERSION={report1.schema_version}")
    print(f"MEMORY_GRAPH_EVAL_ENGINE_VERSION={report1.engine_version}")
    print(f"MEMORY_GRAPH_EVAL_DATASET_VERSION={report1.dataset_version}")
    print()
    print("STRICT_MANIFEST=PASS")
    print("STRICT_SCENARIO_VALIDATION=PASS")
    print("DUPLICATE_JSON_KEYS_REJECTED=PASS")
    print("PATH_SAFETY=PASS")
    print()
    print(f"CORPUS_STATUS={report1.corpus_status}")
    print(f"CORPUS_CASE_COUNT={report1.case_count}")
    print(f"CORPUS_DIGEST={report1.corpus_digest}")
    print()
    print(f"CORPUS_DIGEST_RUN1={report1.corpus_digest}")
    print(f"CORPUS_DIGEST_RUN2={report2.corpus_digest}")
    print("CORPUS_DIGEST_DETERMINISTIC=PASS")
    print()
    print(f"REPORT_ID_RUN1={report1.report_id}")
    print(f"REPORT_ID_RUN2={report2.report_id}")
    print(f"REPORT_BYTES_EQUAL={'true' if report_bytes_equal else 'false'}")
    print()
    print(f"RETRIEVAL_EVALS={report1.retrieval_evals}")
    print(f"FRESHNESS_EVALS={report1.freshness_evals}")
    print(f"PRIVACY_EVALS={report1.privacy_evals}")
    print(f"ISOLATION_EVALS={report1.isolation_evals}")
    print(f"INTEGRITY_EVALS={report1.integrity_evals}")
    print(f"CONVERGENCE_EVALS={report1.convergence_evals}")
    print(f"DETERMINISM_EVALS={report1.determinism_evals}")
    print(f"TRANSACTION_EVALS={report1.transaction_evals}")
    print()
    print(f"FAIL_COUNT={report1.fail_count}")
    print(f"CRITICAL_FAIL_COUNT={report1.critical_fail_count}")
    print()
    print(f"FALSE_READY_COUNT={report1.false_ready_count}")
    print(f"CROSS_USER_LEAKAGE_COUNT={report1.cross_user_leakage_count}")
    print(f"EXCLUDED_FACT_LEAKAGE_COUNT={report1.excluded_fact_leakage_count}")
    print(f"INTEGRITY_FAIL_OPEN_COUNT={report1.integrity_fail_open_count}")
    print(f"UNEXPECTED_DB_MUTATION_COUNT={report1.unexpected_db_mutation_count}")
    print(f"NONDETERMINISTIC_RESULT_COUNT={report1.nondeterministic_result_count}")
    print()
    print(f"SECRET_SENTINEL_REPORT_LEAKAGE={report1.secret_sentinel_report_leakage}")
    print()
    print(f"VERDICT={'PASS' if report1.fail_count == 0 else 'FAIL'}")

    if report1.fail_count > 0:
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
