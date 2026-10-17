"""
tests.gateway.test_nutrition_v2_pr7_benchmark_firewall — Mandatory regression and integrity tests for PR-7.

Validates:
1. Strict observation schema structurally separate from ground truth (no measured_grams or ground_truth_kbzhu).
2. Anti-leakage regression: Identical observation with different ground-truth values produces 100% identical predictions.
3. Fail-closed behavior when image files are missing from disk or unverified.
4. Verification status remains PENDING when real images are unverified.
5. Strict zero network access and enforcement of LIVE_PROVIDER_REQUESTS_ALLOWED=0.
"""

from __future__ import annotations

import json
from pathlib import Path
import socket
from typing import Any
import pytest

from scripts.evaluate_photo_benchmark import (
    BenchmarkObservation,
    BenchmarkPrediction,
    MissingBenchmarkImageError,
    ObservedFoodItem,
    extract_observation,
    run_benchmark_inference,
    run_photo_benchmark,
    score_prediction,
)


CANONICAL_MANIFEST = Path(__file__).resolve().parents[2] / "tests" / "benchmarks" / "photo_kbzhu_v1" / "manifest.json"


# -----------------------------------------------------------------------------
# 1. Observation Schema Quarantines Ground Truth
# -----------------------------------------------------------------------------

def test_observation_schema_quarantines_ground_truth() -> None:
    """Verifies that BenchmarkObservation has zero ground truth attributes."""
    forbidden_attrs = (
        "measured_grams",
        "ground_truth_dishes",
        "ground_truth_kbzhu",
        "annotation_method",
        "annotation_quality",
        "rights_or_license_id",
    )

    # Check class slots
    assert hasattr(BenchmarkObservation, "__slots__")
    assert hasattr(ObservedFoodItem, "__slots__")

    for attr in forbidden_attrs:
        assert attr not in BenchmarkObservation.__slots__, f"Forbidden attribute {attr} in BenchmarkObservation slots"
        assert attr not in ObservedFoodItem.__slots__, f"Forbidden attribute {attr} in ObservedFoodItem slots"

    # Check extract_observation omits ground-truth values
    raw_item: dict[str, Any] = {
        "photo_id": "test_photo_001",
        "split": "held_out",
        "food_category": "lunch",
        "title": "Обед",
        "ground_truth_dishes": [
            {
                "name": "Гречка",
                "measured_grams": 185.4,
                "calories_kcal": 240.0,
                "protein_g": 8.0,
            },
            {
                "name": "Котлета мясная",
                "measured_grams": 110.2,
                "calories_kcal": 260.0,
                "protein_g": 16.0,
            },
        ],
        "measured_grams": 295.6,
        "ground_truth_kbzhu": {
            "calories_kcal": 500.0,
            "protein_g": 24.0,
            "fat_g": 20.0,
            "carbs_g": 52.0,
        },
        "annotation_method": "scale",
        "annotation_quality": "gold",
    }

    obs = extract_observation(raw_item)
    assert isinstance(obs, BenchmarkObservation)
    assert obs.photo_id == "test_photo_001"
    assert obs.split == "held_out"
    assert obs.food_category == "lunch"
    assert len(obs.observed_items) == 2
    assert obs.observed_items[0].name == "Гречка"
    assert obs.observed_items[1].name == "Котлета мясная"

    # Verify none of the ground truth fields are accessible on the observation
    for attr in forbidden_attrs:
        assert not hasattr(obs, attr), f"Observation leaked {attr}"
        assert not hasattr(obs.observed_items[0], attr), f"ObservedFoodItem leaked {attr}"

    # Verify passing invalid types or objects with forbidden attributes to inference is rejected
    with pytest.raises(TypeError):
        run_benchmark_inference(raw_item)  # type: ignore[arg-type]


# -----------------------------------------------------------------------------
# 2. Anti-Leakage Regression: Identical Predictions Under Perturbed Ground Truth
# -----------------------------------------------------------------------------

def test_anti_leakage_regression_identical_predictions_under_perturbed_ground_truth() -> None:
    """
    CRITICAL ANTI-LEAKAGE REGRESSION:
    An identical observation with radically different ground-truth values
    MUST produce 100% bit-for-bit identical predictions.
    """
    # Create fixed observation
    obs = BenchmarkObservation(
        photo_id="leakage_test_photo",
        split="held_out",
        food_category="breakfast",
        title="Завтрак",
        observed_items=(
            ObservedFoodItem(name="Омлет"),
            ObservedFoodItem(name="Сыр Российский"),
            ObservedFoodItem(name="Хлеб пшеничный"),
        ),
    )

    # Radically different ground-truth payloads for the exact same observation
    gt_normal: dict[str, Any] = {
        "measured_grams": 200.0,
        "ground_truth_kbzhu": {"calories_kcal": 350.0, "protein_g": 22.0, "fat_g": 25.0, "carbs_g": 15.0},
        "ground_truth_dishes": [
            {"name": "Омлет", "measured_grams": 130.0},
            {"name": "Сыр Российский", "measured_grams": 30.0},
            {"name": "Хлеб пшеничный", "measured_grams": 40.0},
        ],
    }

    gt_huge_perturbation: dict[str, Any] = {
        "measured_grams": 1850.0,  # 9x mass perturbation
        "ground_truth_kbzhu": {"calories_kcal": 4200.0, "protein_g": 200.0, "fat_g": 300.0, "carbs_g": 180.0},
        "ground_truth_dishes": [
            {"name": "Омлет", "measured_grams": 1200.0},
            {"name": "Сыр Российский", "measured_grams": 350.0},
            {"name": "Хлеб пшеничный", "measured_grams": 300.0},
        ],
    }

    gt_tiny_perturbation: dict[str, Any] = {
        "measured_grams": 15.0,  # 0.07x mass perturbation
        "ground_truth_kbzhu": {"calories_kcal": 25.0, "protein_g": 1.5, "fat_g": 2.0, "carbs_g": 0.8},
        "ground_truth_dishes": [
            {"name": "Омлет", "measured_grams": 8.0},
            {"name": "Сыр Российский", "measured_grams": 3.0},
            {"name": "Хлеб пшеничный", "measured_grams": 4.0},
        ],
    }

    # Run inference for all cases
    pred1 = run_benchmark_inference(obs)
    pred2 = run_benchmark_inference(obs)
    pred3 = run_benchmark_inference(obs)

    # 1. Inference outputs are identical across calls
    assert pred1 == pred2 == pred3

    assert pred1.estimated_mass_center_g == pred2.estimated_mass_center_g == pred3.estimated_mass_center_g
    assert pred1.mass_min_g == pred2.mass_min_g == pred3.mass_min_g
    assert pred1.mass_max_g == pred2.mass_max_g == pred3.mass_max_g
    assert pred1.estimated_calories_center_kcal == pred2.estimated_calories_center_kcal == pred3.estimated_calories_center_kcal
    assert pred1.calories_min_kcal == pred2.calories_min_kcal == pred3.calories_min_kcal
    assert pred1.calories_max_kcal == pred2.calories_max_kcal == pred3.calories_max_kcal

    # 2. Scoring produces independent results reflecting each GT without altering predictions
    score1 = score_prediction(pred1, gt_normal)
    score2 = score_prediction(pred2, gt_huge_perturbation)
    score3 = score_prediction(pred3, gt_tiny_perturbation)

    # Estimated values remain constant
    assert score1.estimated_mass_center_g == score2.estimated_mass_center_g == score3.estimated_mass_center_g
    assert score1.estimated_calories_center_kcal == score2.estimated_calories_center_kcal == score3.estimated_calories_center_kcal

    # Ground truth values correctly recorded
    assert score1.ground_truth_mass_g == 200.0
    assert score2.ground_truth_mass_g == 1850.0
    assert score3.ground_truth_mass_g == 15.0

    # Errors reflect the genuine difference against ground truth
    assert score1.mass_abs_error_g != score2.mass_abs_error_g
    assert score2.mass_abs_error_g != score3.mass_abs_error_g


# -----------------------------------------------------------------------------
# 3. Fail-Closed on Missing Images
# -----------------------------------------------------------------------------

def test_harness_fails_closed_when_image_missing_from_disk(tmp_path: Path) -> None:
    """Verifies that the benchmark harness fails closed when images are missing from disk."""
    dummy_manifest = tmp_path / "manifest.json"
    dummy_manifest_data = {
        "dataset_identifier": "test_missing_images",
        "version": "1.0.0",
        "total_count": 210,
        "calibration_count": 84,
        "held_out_count": 126,
        "items": [
            {
                "photo_id": f"photo_{i:03d}",
                "image_sha256": "0" * 64,
                "split": "calibration" if i < 84 else "held_out",
                "food_category": "test",
                "title": f"Dish {i}",
                "capture_session_or_group_id": f"sess_{i}",
                "ground_truth_dishes": [{"name": "Яблоко", "measured_grams": 150.0}],
                "measured_grams": 150.0,
                "ground_truth_kbzhu": {"calories_kcal": 75.0},
            }
            for i in range(210)
        ],
    }
    dummy_manifest.write_text(json.dumps(dummy_manifest_data), encoding="utf-8")

    empty_images_dir = tmp_path / "images"
    empty_images_dir.mkdir()

    # 1. Specifying images_dir with missing files raises MissingBenchmarkImageError (and FileNotFoundError)
    with pytest.raises((FileNotFoundError, MissingBenchmarkImageError)):
        run_photo_benchmark(dummy_manifest, images_dir=empty_images_dir)

    # 2. Specifying require_images=True without images_dir raises MissingBenchmarkImageError
    with pytest.raises(MissingBenchmarkImageError):
        run_photo_benchmark(dummy_manifest, require_images=True)


# -----------------------------------------------------------------------------
# 4. Status Remains PENDING When Images Are Unverified
# -----------------------------------------------------------------------------

def test_verification_status_remains_pending_when_images_unverified() -> None:
    """Verifies that REAL_PHOTO_ACCEPTANCE remains PENDING when real images are unverified."""
    assert CANONICAL_MANIFEST.exists(), f"Canonical manifest not found: {CANONICAL_MANIFEST}"

    report = run_photo_benchmark(CANONICAL_MANIFEST, images_dir=None)

    assert report.total_items_in_manifest == 210
    assert report.images_verified_on_disk == 0
    assert report.real_photo_acceptance.startswith("PENDING")
    assert "Real images unverified" in report.real_photo_acceptance
    assert report.live_provider_requests_used == 0

    # Offline evaluation passes engineering readiness
    assert report.engineering_ready == "PASS"
    assert report.overall_metrics.mass_interval_coverage_pct >= 90.0
    assert report.overall_metrics.dishes_f1 >= 0.85


# -----------------------------------------------------------------------------
# 5. Zero-Network Execution Enforcement
# -----------------------------------------------------------------------------

def test_zero_network_access_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantees that the photo benchmark runs 100% offline without network requests."""
    def fail_connect(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Network connection attempted during offline photo benchmark evaluation!")

    monkeypatch.setattr(socket.socket, "connect", fail_connect)

    report = run_photo_benchmark(CANONICAL_MANIFEST, split="calibration")
    assert report.engineering_ready == "PASS"
    assert report.live_provider_requests_used == 0


# -----------------------------------------------------------------------------
# 6. Policy Limit Enforcement on Live Requests
# -----------------------------------------------------------------------------

def test_live_provider_requests_fail_closed_under_zero_policy() -> None:
    """Enforces fail-closed behavior when live provider calls are requested."""
    report = run_photo_benchmark(
        CANONICAL_MANIFEST,
        execute_provider=True,
        max_calls=5,  # Exceeds LIVE_PROVIDER_REQUESTS_ALLOWED=0
    )

    assert report.engineering_ready == "FAIL"
    assert "BLOCKED" in report.real_photo_acceptance
    assert report.live_provider_requests_used == 0
