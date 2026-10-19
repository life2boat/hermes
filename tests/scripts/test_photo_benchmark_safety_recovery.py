"""Regression tests for HEALBITE_PRODUCT_CLOSURE_SAFETY_RECOVERY_V1.

Verifies:
1. PhotoBenchmarkReport default dataclass values enforce fail-closed status:
   - product_acceptance == "NOT_PASSED"
   - offline_engineering_acceptance == "NOT_PASSED"
   - public_beta_ready is False
   - real_photo_test_count == 0
   - recognition_error_rate_pct == "NOT_EVALUATED_OFFLINE_MODE"
2. Offline evaluation results:
   - offline_engineering_acceptance == "PASS"
   - product_acceptance == "NOT_PASSED"
   - public_beta_ready is False
   - real_photo_test_count == 0
   - recognition_error_rate_pct == "NOT_EVALUATED_OFFLINE_MODE"
   - offline_testset_database_coverage_pct is populated
   - modeled_clarification_rate_pct is populated
3. JSON output serialization:
   - JSON report contains all safety recovery fields with correct types and values
   - public_beta_ready is strictly false in JSON
   - product_acceptance is strictly "NOT_PASSED"
4. CLI execution:
   - Exit code 0 on valid offline run
   - Stdout prints correct key-value metrics
   - Stdout strictly has PUBLIC_BETA_READY=false and PRODUCT_ACCEPTANCE=NOT_PASSED
   - Stdout never outputs PUBLIC_BETA_READY=true or PRODUCT_ACCEPTANCE=PASS
5. Fail-closed paths:
   - Blocked provider (execute_provider=True, max_calls > 0)
   - Missing photos when require_images=True
   - Image SHA-256 mismatch
   - Insufficient items (< 200 items)
   - Malformed manifest (non-dict, missing items)
   - Session leakage between calibration and held_out
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any
import pytest

from scripts.evaluate_photo_benchmark import (
    BenchmarkMetrics,
    MissingBenchmarkImageError,
    PhotoBenchmarkReport,
    run_photo_benchmark,
)

CANONICAL_MANIFEST = Path(__file__).resolve().parents[2] / "tests" / "benchmarks" / "photo_kbzhu_v1" / "manifest.json"
SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "evaluate_photo_benchmark.py"


# -----------------------------------------------------------------------------
# 1. Dataclass Defaults Invariant
# -----------------------------------------------------------------------------

def test_photo_benchmark_report_default_values_fail_closed() -> None:
    """Verifies that uninitialized / default PhotoBenchmarkReport cannot pass product acceptance."""
    metrics = BenchmarkMetrics(
        total_evaluated=0,
        dishes_precision=0.0,
        dishes_recall=0.0,
        dishes_f1=0.0,
        portion_mae_g=0.0,
        portion_mape_pct=0.0,
        calorie_mae_kcal=0.0,
        calorie_mape_pct=0.0,
        mass_interval_coverage_pct=0.0,
        calorie_interval_coverage_pct=0.0,
    )

    report = PhotoBenchmarkReport(
        dataset_identifier="test",
        version="1.0.0",
        manifest_path="/test",
        split="all",
        total_items_in_manifest=0,
        calibration_count=0,
        held_out_count=0,
        evaluated_count=0,
        images_verified_on_disk=0,
        images_missing_on_disk=0,
        session_leakage_detected=False,
        overall_metrics=metrics,
        calibration_metrics=None,
        held_out_metrics=None,
        engineering_ready="FAIL",
        real_photo_acceptance="PENDING",
        live_provider_requests_used=0,
        summary_message="Test default report",
    )

    assert report.product_acceptance == "NOT_PASSED", "Default product_acceptance MUST be NOT_PASSED"
    assert report.offline_engineering_acceptance == "NOT_PASSED", "Default offline_engineering_acceptance MUST be NOT_PASSED"
    assert report.public_beta_ready is False, "Default public_beta_ready MUST be False"
    assert report.real_photo_test_count == 0, "Default real_photo_test_count MUST be 0"
    assert report.recognition_error_rate_pct == "NOT_EVALUATED_OFFLINE_MODE"
    assert report.offline_testset_database_coverage_pct == 0.0
    assert report.modeled_clarification_rate_pct == 0.0


# -----------------------------------------------------------------------------
# 2. Offline Evaluation Safety Values
# -----------------------------------------------------------------------------

def test_canonical_manifest_offline_evaluation_safety_recovery() -> None:
    """Verifies evaluation of canonical manifest enforces offline safety statuses."""
    assert CANONICAL_MANIFEST.exists(), f"Missing canonical manifest: {CANONICAL_MANIFEST}"

    report = run_photo_benchmark(CANONICAL_MANIFEST, split="calibration")

    # Offline engineering acceptance passes on algorithm & catalog checks
    assert report.offline_engineering_acceptance == "PASS"
    assert report.engineering_ready == "PASS"

    # Product acceptance & public beta readiness MUST remain NOT_PASSED / False
    assert report.product_acceptance == "NOT_PASSED"
    assert report.public_beta_ready is False

    # Real photos have not been evaluated via live vision inference
    assert report.real_photo_test_count == 0
    assert "PENDING" in report.real_photo_acceptance
    assert "BLOCKED_NO_VERIFIED_PHOTO_ASSETS" in report.real_photo_acceptance
    assert report.live_provider_requests_used == 0

    # Recognition error rate is explicitly marked as not evaluated in offline mode
    assert report.recognition_error_rate_pct == "NOT_EVALUATED_OFFLINE_MODE"
    assert report.dish_recognition_f1 == "NOT_EVALUATED_OFFLINE_MODE"

    # Database coverage and clarification proxy metrics are present
    assert report.offline_testset_database_coverage_pct > 90.0
    assert report.database_coverage_pct == report.offline_testset_database_coverage_pct
    assert report.modeled_clarification_rate_pct >= 0.0
    assert report.clarification_rate_pct == report.modeled_clarification_rate_pct


# -----------------------------------------------------------------------------
# 3. JSON Output Serialization
# -----------------------------------------------------------------------------

def test_json_output_serialization_and_fields(tmp_path: Path) -> None:
    """Verifies that writing report JSON preserves all required safety recovery keys and types."""
    out_json = tmp_path / "safety_report.json"

    res = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--manifest",
            str(CANONICAL_MANIFEST),
            "--split",
            "calibration",
            "--output",
            str(out_json),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Benchmark script failed: {res.stderr}"
    assert out_json.exists(), "Output JSON file was not created"

    data = json.loads(out_json.read_text(encoding="utf-8"))

    # Core safety recovery fields
    assert data["product_acceptance"] == "NOT_PASSED"
    assert data["offline_engineering_acceptance"] == "PASS"
    assert data["public_beta_ready"] is False
    assert data["real_photo_test_count"] == 0
    assert data["recognition_error_rate_pct"] == "NOT_EVALUATED_OFFLINE_MODE"
    assert "offline_testset_database_coverage_pct" in data
    assert "modeled_clarification_rate_pct" in data
    assert data["offline_testset_database_coverage_pct"] > 90.0


# -----------------------------------------------------------------------------
# 4. CLI Stdout Token Invariants
# -----------------------------------------------------------------------------

def test_cli_stdout_tokens_and_prohibited_claims() -> None:
    """Verifies CLI console and [METRICS] output format, ensuring no false product claims."""
    res = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--manifest",
            str(CANONICAL_MANIFEST),
            "--split",
            "calibration",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Script failed: {res.stderr}"
    stdout = res.stdout

    # Mandatory positive tokens
    assert "OFFLINE_ENGINEERING_ACCEPTANCE=PASS" in stdout
    assert "PRODUCT_ACCEPTANCE=NOT_PASSED" in stdout
    assert "PUBLIC_BETA_READY=false" in stdout
    assert "RECOGNITION_ERROR_RATE_PCT=NOT_EVALUATED_OFFLINE_MODE" in stdout
    assert "REAL_PHOTO_TEST_COUNT=0" in stdout
    assert "OFFLINE_TESTSET_DATABASE_COVERAGE_PCT=" in stdout
    assert "MODELED_CLARIFICATION_RATE_PCT=" in stdout
    assert "DATABASE_COVERAGE_PCT=" in stdout
    assert "(offline testset coverage)" in stdout
    assert "CLARIFICATION_RATE_PCT=" in stdout
    assert "(modeled proxy; not actual user frequency)" in stdout
    assert "PRODUCTION_CHANGED=NO" in stdout

    # Strictly forbidden tokens
    assert "PUBLIC_BETA_READY=true" not in stdout, "CLI must NEVER claim PUBLIC_BETA_READY=true in offline mode"
    assert "PRODUCT_ACCEPTANCE=PASS" not in stdout, "CLI must NEVER claim PRODUCT_ACCEPTANCE=PASS in offline mode"
    assert "RECOGNITION_ERROR_RATE_PCT=0.00" not in stdout, "Must NOT claim 0.00% error rate when dish names from manifest"


# -----------------------------------------------------------------------------
# 5. Fail-Closed Paths
# -----------------------------------------------------------------------------

def test_fail_closed_on_blocked_provider() -> None:
    """Verifies that requesting live provider calls fails closed immediately."""
    report = run_photo_benchmark(
        CANONICAL_MANIFEST,
        execute_provider=True,
        max_calls=10,
    )

    assert report.engineering_ready == "FAIL"
    assert report.offline_engineering_acceptance == "FAIL"
    assert report.product_acceptance == "NOT_PASSED"
    assert report.public_beta_ready is False
    assert report.real_photo_test_count == 0
    assert "BLOCKED" in report.real_photo_acceptance
    assert report.live_provider_requests_used == 0


def test_fail_closed_on_missing_photos_when_required(tmp_path: Path) -> None:
    """Verifies that require_images=True without images raises MissingBenchmarkImageError."""
    with pytest.raises(MissingBenchmarkImageError):
        run_photo_benchmark(CANONICAL_MANIFEST, require_images=True)

    # CLI also returns non-zero
    res = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--manifest",
            str(CANONICAL_MANIFEST),
            "--require-images",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 1
    assert "missing" in res.stderr.lower() or "error" in res.stderr.lower()


def test_fail_closed_on_image_sha256_mismatch(tmp_path: Path) -> None:
    """Verifies that corrupted image binary with SHA-256 mismatch raises ValueError."""
    fake_img = tmp_path / "images" / "test_dish_001.jpg"
    fake_img.parent.mkdir(parents=True, exist_ok=True)
    fake_img.write_bytes(b"corrupted image content")

    manifest_data = {
        "dataset_identifier": "test_sha_mismatch",
        "version": "1.0.0",
        "total_count": 210,
        "calibration_count": 84,
        "held_out_count": 126,
        "items": [
            {
                "photo_id": "test_dish_001",
                "image_sha256": "e" * 64,  # Mismatched SHA256
                "split": "calibration",
                "food_category": "soup",
                "title": "Борщ",
                "capture_session_or_group_id": "sess_001",
                "ground_truth_dishes": [{"name": "Борщ", "measured_grams": 250.0}],
                "measured_grams": 250.0,
                "ground_truth_kbzhu": {"calories_kcal": 150.0},
            }
        ] + [
            {
                "photo_id": f"dummy_{i:03d}",
                "image_sha256": "a" * 64,
                "split": "calibration" if i < 83 else "held_out",
                "food_category": "soup",
                "title": f"Dish {i}",
                "capture_session_or_group_id": f"sess_{i:03d}",
                "ground_truth_dishes": [{"name": "Борщ", "measured_grams": 250.0}],
                "measured_grams": 250.0,
                "ground_truth_kbzhu": {"calories_kcal": 150.0},
            }
            for i in range(209)
        ],
    }

    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(ValueError, match="Image SHA256 mismatch"):
        run_photo_benchmark(manifest_file, images_dir=fake_img.parent)


def test_fail_closed_on_malformed_manifest(tmp_path: Path) -> None:
    """Verifies that malformed manifests fail closed with ValueError."""
    # 1. Manifest is a JSON list instead of an object
    list_manifest = tmp_path / "list_manifest.json"
    list_manifest.write_text(json.dumps([{"item": 1}]), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        run_photo_benchmark(list_manifest)

    # 2. Manifest lacks 'items' key
    no_items_manifest = tmp_path / "no_items.json"
    no_items_manifest.write_text(json.dumps({"dataset_identifier": "bad"}), encoding="utf-8")
    with pytest.raises(ValueError, match="'items'"):
        run_photo_benchmark(no_items_manifest)

    # 3. Manifest has insufficient items (< 200)
    few_items_manifest = tmp_path / "few_items.json"
    few_items_manifest.write_text(json.dumps({"items": [{"photo_id": "1"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="at least 200 items"):
        run_photo_benchmark(few_items_manifest)


def test_fail_closed_on_session_leakage(tmp_path: Path) -> None:
    """Verifies that cross-split session contamination raises ValueError."""
    manifest_data = {
        "dataset_identifier": "test_leakage",
        "version": "1.0.0",
        "total_count": 210,
        "calibration_count": 84,
        "held_out_count": 126,
        "items": [
            {
                "photo_id": f"photo_{i:03d}",
                "image_sha256": "0" * 64,
                "split": "calibration" if i < 84 else "held_out",
                "food_category": "lunch",
                "title": f"Dish {i}",
                "capture_session_or_group_id": "SHARED_SESSION" if i in (10, 100) else f"sess_{i}",
                "ground_truth_dishes": [{"name": "Гречка", "measured_grams": 100.0}],
                "measured_grams": 100.0,
                "ground_truth_kbzhu": {"calories_kcal": 110.0},
            }
            for i in range(210)
        ],
    }

    manifest_file = tmp_path / "leakage_manifest.json"
    manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

    with pytest.raises(ValueError, match="Session leakage detected"):
        run_photo_benchmark(manifest_file)
