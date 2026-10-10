#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.healbite_food_vision_benchmark import (
    SCORER_V1_LEGACY,
    SCORER_V2_EXPLICIT_ALIAS_COMPOUND,
    load_historical_responses,
    load_manifest_image_map,
    load_scorer_v2_contract,
    render_model_comparison_markdown,
    replay_historical_model,
)


DEFAULT_MANIFEST = "/home/hermes/benchmarks/s71v2-r7d-b/manifest.json"
DEFAULT_SOURCES = {
    "qwen3.6-plus": "/home/hermes/evidence/s71v2-r7f-q2-b-qwen36plus/20260710T043420Z/benchmark_metrics.json",
    "qwen3.7-plus": "/home/hermes/evidence/s71v2-r7f-q1-qwen-nextgen/20260709T161257Z/benchmark_metrics.json",
    "qwen3-vl-8b-instruct": "/home/hermes/evidence/s71v2-r7e-c1-limited-rebenchmark/20260709T134049Z/qwen_metrics.json",
}


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _test_results_markdown(pytest_cmd: list[str], secret_cmd: list[str]) -> str:
    return "\n".join(
        [
            "# Validation",
            "",
            "## Focused tests",
            "",
            f"```text\n{' '.join(pytest_cmd)}\n```",
            "",
            "## Secret scan",
            "",
            f"```text\n{' '.join(secret_cmd)}\n```",
            "",
        ]
    ) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay canonical food-vision benchmark scoring without provider access.")
    parser.add_argument("--manifest", default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="MODEL_ID=PATH override. Can be specified multiple times.",
    )
    parser.add_argument("--test-command", nargs="*", default=[])
    parser.add_argument("--secret-command", nargs="*", default=[])
    args = parser.parse_args()

    sources = dict(DEFAULT_SOURCES)
    for item in args.source:
        model_id, _, path = item.partition("=")
        if not model_id or not path:
            raise SystemExit(f"invalid --source: {item!r}")
        sources[model_id] = path

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)

    manifest_map = load_manifest_image_map(args.manifest)
    contract = load_scorer_v2_contract()
    legacy = {}
    scorer_v2 = {}
    for model_id, path in sources.items():
        historical = load_historical_responses(path)
        legacy[model_id] = replay_historical_model(
            model_id=model_id,
            historical_data=historical,
            manifest_map=manifest_map,
            scorer_version=SCORER_V1_LEGACY,
        )
        scorer_v2[model_id] = replay_historical_model(
            model_id=model_id,
            historical_data=historical,
            manifest_map=manifest_map,
            scorer_version=SCORER_V2_EXPLICIT_ALIAS_COMPOUND,
            contract=contract,
        )

    summary = {
        "provider_free_scorer_v2_replay": True,
        "scorer_v1_result_valid": True,
        "scorer_v1_contract_too_narrow": True,
        "canonical_repository_scorer_missing_historically": True,
        "canonical_repository_scorer_present_now": True,
        "historical_metrics_preserved": True,
        "automatic_production_selection": False,
        "deployment_authorized": False,
        "current_hermes_runtime_proven": False,
        "models": {
            model_id: {
                "historical_replay_complete": scorer_v2[model_id]["historical_replay_complete"],
                "quality_gate_result": scorer_v2[model_id]["quality_gate_result"],
                "v1_exact_reproduction": legacy[model_id]["exact_v1_reproduction"],
                "v2_major_component_precision": scorer_v2[model_id]["aggregate_metrics"]["major_component_precision"],
                "v2_major_component_recall": scorer_v2[model_id]["aggregate_metrics"]["major_component_recall"],
                "v2_sauce_recall": scorer_v2[model_id]["aggregate_metrics"]["sauce_recall"],
            }
            for model_id in sources
        },
    }
    request_accounting = {
        "provider_requests": 0,
        "network_provider_probes": 0,
        "production_db_opens": 0,
        "production_db_writes": 0,
        "qdrant_requests": 0,
    }
    quality_gate_decision = {
        "automatic_production_selection": False,
        "deployment_authorized": False,
        "current_hermes_runtime_proven": False,
        "models": {model_id: scorer_v2[model_id]["quality_gate_result"] for model_id in sources},
    }

    _write_json(output_dir / "summary.json", summary)
    _write_json(output_dir / "scorer_contract.json", contract)
    _write_json(output_dir / "legacy_replay.json", legacy)
    _write_json(output_dir / "scorer_v2_replay.json", scorer_v2)
    _write_json(output_dir / "request_accounting.json", request_accounting)

    comparison = render_model_comparison_markdown(legacy, scorer_v2)
    (output_dir / "model_comparison.md").write_text(comparison, encoding="utf-8")
    os.chmod(output_dir / "model_comparison.md", 0o600)

    decision_md = "\n".join(
        [
            "# Quality Gate Decision",
            "",
            "- provider_requests=0",
            "- network_provider_probes=0",
            "- automatic_production_selection=false",
            "- deployment_authorized=false",
            "- current_hermes_runtime_proven=false",
            "",
            "## Model results",
            "",
            *[
                f"- {model_id}: {gate}"
                for model_id, gate in quality_gate_decision["models"].items()
            ],
            "",
        ]
    ) + "\n"
    (output_dir / "quality_gate_decision.md").write_text(decision_md, encoding="utf-8")
    os.chmod(output_dir / "quality_gate_decision.md", 0o600)

    test_results = _test_results_markdown(args.test_command, args.secret_command)
    (output_dir / "test_results.md").write_text(test_results, encoding="utf-8")
    os.chmod(output_dir / "test_results.md", 0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
