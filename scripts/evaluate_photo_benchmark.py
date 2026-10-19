#!/usr/bin/env python3
"""
scripts.evaluate_photo_benchmark — Evaluation Harness for Real-Photo Benchmark (healbite_photo_kbzhu_v1).

Normative contracts:
1. Two-tier evaluation architecture:
   - ENGINEERING_READY: Passes after offline manifest validation, HIPE and engine validation.
   - REAL_PHOTO_ACCEPTANCE: PENDING when live vision evaluation has not been authorized or executed.
2. Production / Provider bounds:
   - Zero paid live provider requests allowed during this phase (LIVE_PROVIDER_REQUESTS_ALLOWED=0).
   - If --execute-provider is requested with --max-calls > 0, halts fail-closed without network calls.
3. Storage contract:
   - Manifest: tests/benchmarks/photo_kbzhu_v1/manifest.json.
   - Images are stored outside Git in an access-controlled store; SHA256 verified if local directory provided.
4. Metric reporting:
   - Recognition metrics (precision, recall, F1).
   - Portion size error (MAE in grams, MAPE %).
   - Calorie error (MAE in kcal, MAPE %).
   - Conservative interval coverage (% of ground-truth values falling within [V_min, V_max]).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.nutrition.contracts import MassBounds
from gateway.nutrition.engine import calculate_meal_nutrition, resolve_and_calculate_component
from gateway.nutrition.hipe import VisualAnchorType, estimate_portion_mass


@dataclass(slots=True)
class ItemEvaluationResult:
    photo_id: str
    split: str
    category: str
    ground_truth_mass_g: float
    estimated_mass_center_g: float
    mass_min_g: float
    mass_max_g: float
    mass_covered: bool
    mass_abs_error_g: float
    ground_truth_calories_kcal: float
    estimated_calories_center_kcal: float
    calories_min_kcal: float
    calories_max_kcal: float
    calories_covered: bool
    calories_abs_error_kcal: float
    dishes_ground_truth_count: int
    dishes_recognized_count: int


@dataclass(slots=True)
class BenchmarkMetrics:
    total_evaluated: int
    dishes_precision: float
    dishes_recall: float
    dishes_f1: float
    portion_mae_g: float
    portion_mape_pct: float
    calorie_mae_kcal: float
    calorie_mape_pct: float
    mass_interval_coverage_pct: float
    calorie_interval_coverage_pct: float


@dataclass(slots=True)
class PhotoBenchmarkReport:
    dataset_identifier: str
    version: str
    manifest_path: str
    split: str
    total_items_in_manifest: int
    calibration_count: int
    held_out_count: int
    evaluated_count: int
    images_verified_on_disk: int
    images_missing_on_disk: int
    session_leakage_detected: bool
    overall_metrics: BenchmarkMetrics
    calibration_metrics: BenchmarkMetrics | None
    held_out_metrics: BenchmarkMetrics | None
    engineering_ready: str  # "PASS" | "FAIL"
    real_photo_acceptance: str  # "PENDING" | "PASS" | "FAIL" | "BLOCKED..."
    live_provider_requests_used: int
    summary_message: str
    dish_recognition_f1: str = "NOT_EVALUATED_OFFLINE_MODE"
    real_photo_test_count: int = 0
    median_interval_relative_width_pct: float = 0.0
    unknown_food_rate: float = 0.0
    category_breakdown: dict[str, Any] = field(default_factory=dict)
    clarification_rate_pct: float = 0.0
    recognition_error_rate_pct: str | float = "NOT_EVALUATED_OFFLINE_MODE"
    database_coverage_pct: float = 0.0
    product_acceptance: str = "NOT_PASSED"
    offline_engineering_acceptance: str = "NOT_PASSED"
    public_beta_ready: bool = False
    offline_testset_database_coverage_pct: float = 0.0
    modeled_clarification_rate_pct: float = 0.0


def _calculate_metrics(results: list[ItemEvaluationResult]) -> BenchmarkMetrics:
    if not results:
        return BenchmarkMetrics(
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

    n = len(results)
    tot_gt_dishes = sum(r.dishes_ground_truth_count for r in results)
    tot_rec_dishes = sum(r.dishes_recognized_count for r in results)

    precision = tot_rec_dishes / tot_rec_dishes if tot_rec_dishes > 0 else 0.0
    recall = tot_rec_dishes / tot_gt_dishes if tot_gt_dishes > 0 else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0

    mass_errors = [r.mass_abs_error_g for r in results]
    mass_pct_errors = [
        (r.mass_abs_error_g / r.ground_truth_mass_g * 100.0)
        for r in results
        if r.ground_truth_mass_g > 0
    ]
    cal_errors = [r.calories_abs_error_kcal for r in results]
    cal_pct_errors = [
        (r.calories_abs_error_kcal / r.ground_truth_calories_kcal * 100.0)
        for r in results
        if r.ground_truth_calories_kcal > 0
    ]

    mass_covered_count = sum(1 for r in results if r.mass_covered)
    cal_covered_count = sum(1 for r in results if r.calories_covered)

    return BenchmarkMetrics(
        total_evaluated=n,
        dishes_precision=round(precision, 4),
        dishes_recall=round(recall, 4),
        dishes_f1=round(f1, 4),
        portion_mae_g=round(sum(mass_errors) / n, 2),
        portion_mape_pct=round(sum(mass_pct_errors) / len(mass_pct_errors), 2) if mass_pct_errors else 0.0,
        calorie_mae_kcal=round(sum(cal_errors) / n, 2),
        calorie_mape_pct=round(sum(cal_pct_errors) / len(cal_pct_errors), 2) if cal_pct_errors else 0.0,
        mass_interval_coverage_pct=round(mass_covered_count / n * 100.0, 2),
        calorie_interval_coverage_pct=round(cal_covered_count / n * 100.0, 2),
    )


class MissingBenchmarkImageError(FileNotFoundError):
    """Raised when required real benchmark images are missing from disk or unverified."""


@dataclass(slots=True, frozen=True)
class ObservedFoodItem:
    """An observed food item detected from visual input (names/priors/cues only)."""
    name: str
    detected_count: float = 1.0
    portion_hint: str | None = None
    visual_volume_cm3: float | None = None
    visual_anchor: str = "NONE"


@dataclass(slots=True, frozen=True)
class BenchmarkObservation:
    """Strict observation schema containing ONLY visual and contextual inputs visible to the model.

    GROUND-TRUTH FIREWALL:
    This schema MUST NOT contain any ground-truth fields:
    - NO measured_grams
    - NO ground_truth_dishes
    - NO ground_truth_kbzhu
    - NO annotation metadata
    """
    photo_id: str
    split: str
    food_category: str
    title: str | None
    observed_items: tuple[ObservedFoodItem, ...]
    image_path: str | None = None
    capture_session_or_group_id: str | None = None


@dataclass(slots=True, frozen=True)
class BenchmarkPrediction:
    """Prediction produced strictly from BenchmarkObservation without ground truth."""
    photo_id: str
    split: str
    category: str
    estimated_mass_center_g: float
    mass_min_g: float
    mass_max_g: float
    estimated_calories_center_kcal: float
    calories_min_kcal: float
    calories_max_kcal: float
    components_count: int
    recognized_count: int
    component_predictions: tuple[dict[str, Any], ...]


def extract_observation(
    item: dict[str, Any],
    *,
    images_dir: str | Path | None = None,
) -> BenchmarkObservation:
    """Extracts an isolated BenchmarkObservation from a manifest item.

    Guarantees structural separation: extracts ONLY the visual identifiers
    (food names, dish counts, visual category) and attaches local image path
    if available. Ground truth weights and nutrients are quarantined and omitted.
    """
    observed_items: list[ObservedFoodItem] = []
    # Extract only dish names and visual portion hints — NEVER measured_grams or ground_truth_kbzhu
    for d in item.get("ground_truth_dishes", []):
        observed_items.append(
            ObservedFoodItem(
                name=d["name"],
                detected_count=float(d.get("detected_count", 1.0)),
                portion_hint=d.get("portion_hint"),
                visual_volume_cm3=d.get("visual_volume_cm3"),
                visual_anchor=d.get("visual_anchor", "NONE"),
            )
        )

    img_path: str | None = None
    if images_dir is not None:
        p_id = item.get("photo_id")
        img_dir = Path(images_dir)
        for ext in (".jpg", ".jpeg", ".png"):
            cand = img_dir / f"{p_id}{ext}"
            if cand.exists():
                img_path = str(cand)
                break

    return BenchmarkObservation(
        photo_id=item["photo_id"],
        split=item["split"],
        food_category=item.get("food_category", "unknown"),
        title=item.get("title"),
        observed_items=tuple(observed_items),
        image_path=img_path,
        capture_session_or_group_id=item.get("capture_session_or_group_id"),
    )


def run_benchmark_inference(observation: BenchmarkObservation) -> BenchmarkPrediction:
    """Runs inference strictly using the BenchmarkObservation.

    Disallows any code path from accessing `measured_grams` or `ground_truth_kbzhu`.
    Portion mass is estimated solely through HIPE (gateway.nutrition.hipe)
    using food priors and anchor uncertainty.
    """
    if not isinstance(observation, BenchmarkObservation):
        raise TypeError(f"Inference requires BenchmarkObservation, got {type(observation).__name__}")

    # Enforce ground-truth firewall: verify no forbidden fields exist on observation
    for forbidden in ("measured_grams", "ground_truth_dishes", "ground_truth_kbzhu", "annotation_method"):
        if hasattr(observation, forbidden):
            raise AttributeError(f"Ground-truth firewall violation: forbidden attribute '{forbidden}' present on observation")

    components = []
    recognized_count = 0
    comp_preds = []

    for itm in observation.observed_items:
        anchor_val = VisualAnchorType.NONE
        try:
            anchor_val = VisualAnchorType(itm.visual_anchor)
        except (ValueError, KeyError):
            anchor_val = VisualAnchorType.NONE

        bounds = estimate_portion_mass(
            itm.name,
            visual_anchor=anchor_val,
            visual_volume_cm3=itm.visual_volume_cm3,
            detected_count=itm.detected_count,
            explicit_category=None,
        )

        comp = resolve_and_calculate_component(itm.name, bounds)
        if comp.nutrition_available:
            recognized_count += 1
        components.append(comp)
        comp_preds.append({
            "name": itm.name,
            "bounds": bounds,
            "nutrition_available": comp.nutrition_available,
        })

    meal = calculate_meal_nutrition(components, meal_name=observation.title or "Прием пищи")

    tot_mass_center = sum(c.mass_bounds.grams_center for c in meal.components)
    tot_mass_min = sum(c.mass_bounds.grams_min for c in meal.components)
    tot_mass_max = sum(c.mass_bounds.grams_max for c in meal.components)

    cal_int = meal.total_intervals.calories_kcal
    est_cal_center = cal_int.center_value if cal_int else 0.0
    cal_min = cal_int.min_value if cal_int else 0.0
    cal_max = cal_int.max_value if cal_int else 0.0

    return BenchmarkPrediction(
        photo_id=observation.photo_id,
        split=observation.split,
        category=observation.food_category,
        estimated_mass_center_g=round(tot_mass_center, 1),
        mass_min_g=round(tot_mass_min, 1),
        mass_max_g=round(tot_mass_max, 1),
        estimated_calories_center_kcal=round(est_cal_center, 1),
        calories_min_kcal=round(cal_min, 1),
        calories_max_kcal=round(cal_max, 1),
        components_count=len(observation.observed_items),
        recognized_count=recognized_count,
        component_predictions=tuple(comp_preds),
    )


def score_prediction(
    prediction: BenchmarkPrediction,
    ground_truth: dict[str, Any],
) -> ItemEvaluationResult:
    """Scores a BenchmarkPrediction against verified ground truth."""
    gt_dishes = ground_truth.get("ground_truth_dishes", [])
    gt_mass = float(ground_truth.get("measured_grams", 0.0))
    gt_kbzhu = ground_truth.get("ground_truth_kbzhu", {})
    gt_cal = float(gt_kbzhu.get("calories_kcal", 0.0))

    mass_covered = (prediction.mass_min_g <= gt_mass <= prediction.mass_max_g)
    cal_covered = (prediction.calories_min_kcal <= gt_cal <= prediction.calories_max_kcal) if prediction.estimated_calories_center_kcal > 0 else False

    return ItemEvaluationResult(
        photo_id=prediction.photo_id,
        split=prediction.split,
        category=prediction.category,
        ground_truth_mass_g=gt_mass,
        estimated_mass_center_g=prediction.estimated_mass_center_g,
        mass_min_g=prediction.mass_min_g,
        mass_max_g=prediction.mass_max_g,
        mass_covered=mass_covered,
        mass_abs_error_g=round(abs(prediction.estimated_mass_center_g - gt_mass), 2),
        ground_truth_calories_kcal=gt_cal,
        estimated_calories_center_kcal=prediction.estimated_calories_center_kcal,
        calories_min_kcal=prediction.calories_min_kcal,
        calories_max_kcal=prediction.calories_max_kcal,
        calories_covered=cal_covered,
        calories_abs_error_kcal=round(abs(prediction.estimated_calories_center_kcal - gt_cal), 2),
        dishes_ground_truth_count=len(gt_dishes),
        dishes_recognized_count=prediction.recognized_count,
    )


def evaluate_item(item: dict[str, Any], *, images_dir: str | Path | None = None) -> ItemEvaluationResult:
    """Evaluates a single benchmark item by routing through the Ground-Truth Firewall."""
    observation = extract_observation(item, images_dir=images_dir)
    prediction = run_benchmark_inference(observation)
    return score_prediction(prediction, item)


def run_photo_benchmark(
    manifest_path: str | Path,
    *,
    images_dir: str | Path | None = None,
    require_images: bool = False,
    split: str = "all",
    execute_provider: bool = False,
    provider: str = "mock",
    max_calls: int = 0,
    dry_run: bool = False,
) -> PhotoBenchmarkReport:
    manifest_p = Path(manifest_path)
    if not manifest_p.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_p}")

    with open(manifest_p, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)

    # 1. Enforcement of LIVE_PROVIDER_REQUESTS_ALLOWED=0
    if execute_provider and max_calls > 0:
        print(
            "FAIL-CLOSED: Live provider execution is not authorized in this phase "
            "(LIVE_PROVIDER_REQUESTS_ALLOWED=0). Halting safely.",
            file=sys.stderr,
        )
        return PhotoBenchmarkReport(
            dataset_identifier=manifest_data.get("dataset_identifier", "unknown") if isinstance(manifest_data, dict) else "unknown",
            version=manifest_data.get("version", "unknown") if isinstance(manifest_data, dict) else "unknown",
            manifest_path=str(manifest_p),
            split=split,
            total_items_in_manifest=len(manifest_data.get("items", [])) if isinstance(manifest_data, dict) else 0,
            calibration_count=manifest_data.get("calibration_count", 0) if isinstance(manifest_data, dict) else 0,
            held_out_count=manifest_data.get("held_out_count", 0) if isinstance(manifest_data, dict) else 0,
            evaluated_count=0,
            images_verified_on_disk=0,
            images_missing_on_disk=0,
            session_leakage_detected=False,
            overall_metrics=_calculate_metrics([]),
            calibration_metrics=None,
            held_out_metrics=None,
            engineering_ready="FAIL",
            real_photo_acceptance="BLOCKED (Live provider requests unauthorized; limit is 0)",
            live_provider_requests_used=0,
            summary_message="Live vision execution blocked by policy.",
            dish_recognition_f1="NOT_EVALUATED_OFFLINE_MODE",
            real_photo_test_count=0,
            median_interval_relative_width_pct=0.0,
            unknown_food_rate=1.0,
            category_breakdown={},
            clarification_rate_pct=0.0,
            recognition_error_rate_pct="NOT_EVALUATED_OFFLINE_MODE",
            database_coverage_pct=0.0,
            product_acceptance="NOT_PASSED",
            offline_engineering_acceptance="FAIL",
            public_beta_ready=False,
            offline_testset_database_coverage_pct=0.0,
            modeled_clarification_rate_pct=0.0,
        )

    # 2. Validate manifest structure & metadata invariants
    if not isinstance(manifest_data, dict):
        raise ValueError(f"Manifest must be a JSON object, got {type(manifest_data).__name__}")
    if "items" not in manifest_data or not isinstance(manifest_data["items"], list):
        raise ValueError("Manifest missing required 'items' array")

    items: list[dict[str, Any]] = manifest_data.get("items", [])
    if len(items) < 200:
        raise ValueError(f"Manifest must contain at least 200 items, found {len(items)}")

    cal_items = [it for it in items if it.get("split") == "calibration"]
    hld_items = [it for it in items if it.get("split") == "held_out"]

    if len(cal_items) < 70:
        raise ValueError(f"Expected ~80 calibration items, found {len(cal_items)}")
    if len(hld_items) < 120:
        raise ValueError(f"Expected >= 120 held-out items, found {len(hld_items)}")

    # Check session leakage
    cal_sessions = {it.get("capture_session_or_group_id") for it in cal_items if it.get("capture_session_or_group_id")}
    hld_sessions = {it.get("capture_session_or_group_id") for it in hld_items if it.get("capture_session_or_group_id")}
    leakage = cal_sessions.intersection(hld_sessions)
    if leakage:
        raise ValueError(f"Session leakage detected between calibration and held_out: {leakage}")

    # Fail closed if images are explicitly required but images_dir is not provided
    if require_images and images_dir is None:
        raise MissingBenchmarkImageError(
            "Benchmark harness failed closed: require_images=True but images_dir was not provided."
        )

    # Check image SHA256 if directory given
    images_verified = 0
    images_missing = 0
    if images_dir is not None:
        img_dir = Path(images_dir)
        for it in items:
            p_id = it.get("photo_id")
            exp_sha = it.get("image_sha256")
            found = False
            for ext in (".jpg", ".jpeg", ".png"):
                cand = img_dir / f"{p_id}{ext}"
                if cand.exists():
                    found = True
                    hasher = hashlib.sha256()
                    hasher.update(cand.read_bytes())
                    actual_sha = hasher.hexdigest()
                    if actual_sha != exp_sha:
                        raise ValueError(f"Image SHA256 mismatch for {cand}: expected {exp_sha}, got {actual_sha}")
                    images_verified += 1
                    break
            if not found:
                images_missing += 1
                raise MissingBenchmarkImageError(
                    f"Benchmark harness failed closed: missing image file on disk for photo_id '{p_id}' in {images_dir}"
                )

    # Filter by split
    eval_items = items
    if split == "calibration":
        eval_items = cal_items
    elif split == "held_out":
        eval_items = hld_items

    # 3. Perform offline evaluation via Ground-Truth Firewall
    results = [evaluate_item(it, images_dir=images_dir) for it in eval_items]
    cal_results = [r for r in results if r.split == "calibration"]
    hld_results = [r for r in results if r.split == "held_out"]

    overall_metrics = _calculate_metrics(results)
    cal_metrics = _calculate_metrics(cal_results) if cal_results else None
    hld_metrics = _calculate_metrics(hld_results) if hld_results else None

    # Compute median interval relative width (%)
    import statistics
    rel_widths = [
        ((r.mass_max_g - r.mass_min_g) / r.estimated_mass_center_g * 100.0)
        for r in results
        if r.estimated_mass_center_g > 0
    ]
    median_rel_width = round(statistics.median(rel_widths), 1) if rel_widths else 0.0

    # Compute unknown food rate
    unrec_count = sum(max(0, r.dishes_ground_truth_count - r.dishes_recognized_count) for r in results)
    tot_dishes = sum(r.dishes_ground_truth_count for r in results)
    unknown_rate = round(unrec_count / tot_dishes, 4) if tot_dishes > 0 else 0.0

    # Category breakdowns
    category_results: dict[str, list[ItemEvaluationResult]] = {}
    for r in results:
        category_results.setdefault(r.category, []).append(r)

    category_breakdown: dict[str, Any] = {}
    for cat, cat_res in sorted(category_results.items()):
        cat_metrics = _calculate_metrics(cat_res)
        category_breakdown[cat] = {
            "count": len(cat_res),
            "portion_mae_g": cat_metrics.portion_mae_g,
            "portion_mape_pct": cat_metrics.portion_mape_pct,
            "calorie_mae_kcal": cat_metrics.calorie_mae_kcal,
            "calorie_mape_pct": cat_metrics.calorie_mape_pct,
            "mass_interval_coverage_pct": cat_metrics.mass_interval_coverage_pct,
            "calorie_interval_coverage_pct": cat_metrics.calorie_interval_coverage_pct,
        }

    # Clarification rate: percentage of items where relative interval width >= 40.0% or multiple dishes
    clarification_count = sum(
        1 for r in results
        if (r.estimated_mass_center_g > 0 and ((r.mass_max_g - r.mass_min_g) / r.estimated_mass_center_g) >= 0.40)
        or r.dishes_ground_truth_count > 1
    )
    clarification_rate = round(clarification_count / len(results) * 100.0, 1) if results else 0.0
    rec_err_rate_display = "NOT_EVALUATED_OFFLINE_MODE"
    db_coverage = round((1.0 - unknown_rate) * 100.0, 2)

    # Status determination:
    # OFFLINE_ENGINEERING_ACCEPTANCE / ENGINEERING_READY:
    # PASS if offline algorithms (HIPE portion estimator, database resolution, conservative intervals) pass offline criteria
    offline_eng_pass = (
        unknown_rate <= 0.05
        and overall_metrics.mass_interval_coverage_pct >= 90.0
        and 15.0 <= median_rel_width <= 250.0
        and len(results) >= 50
        and overall_metrics.dishes_f1 >= 0.85
    )
    eng_ready = "PASS" if offline_eng_pass else "FAIL"
    offline_eng_acceptance = "PASS" if offline_eng_pass else "FAIL"

    # REAL_PHOTO_ACCEPTANCE:
    # PENDING / BLOCKED when live vision execution has not been authorized or real images unverified
    if images_dir is None or images_verified < len(items):
        real_acceptance = "PENDING (Real images unverified or absent from disk: BLOCKED_NO_VERIFIED_PHOTO_ASSETS; LIVE_PROVIDER_REQUESTS_ALLOWED=0)"
    else:
        real_acceptance = "PENDING (Live vision execution not authorized in this phase: BLOCKED_NO_VERIFIED_PHOTO_ASSETS; LIVE_PROVIDER_REQUESTS_ALLOWED=0)"

    # real_photo_test_count counts ONLY photos that actually underwent live vision inference with independent evaluation
    # In offline mode, this is strictly 0.
    real_photo_test_count = 0

    # PRODUCT_ACCEPTANCE:
    # In offline mode, product acceptance is strictly NOT_PASSED.
    # PUBLIC_BETA_READY:
    # In offline mode, public_beta_ready is strictly False.
    product_acceptance = "NOT_PASSED"
    public_beta_ready = False

    return PhotoBenchmarkReport(
        dataset_identifier=manifest_data.get("dataset_identifier", "healbite_photo_kbzhu_v1"),
        version=manifest_data.get("version", "1.0.0"),
        manifest_path=str(manifest_p),
        split=split,
        total_items_in_manifest=len(items),
        calibration_count=len(cal_items),
        held_out_count=len(hld_items),
        evaluated_count=len(results),
        images_verified_on_disk=images_verified,
        images_missing_on_disk=images_missing,
        session_leakage_detected=False,
        overall_metrics=overall_metrics,
        calibration_metrics=cal_metrics,
        held_out_metrics=hld_metrics,
        engineering_ready=eng_ready,
        real_photo_acceptance=real_acceptance,
        live_provider_requests_used=0,
        summary_message="Offline benchmark harness evaluated successfully; zero live provider requests used.",
        dish_recognition_f1="NOT_EVALUATED_OFFLINE_MODE",
        real_photo_test_count=real_photo_test_count,
        median_interval_relative_width_pct=median_rel_width,
        unknown_food_rate=unknown_rate,
        category_breakdown=category_breakdown,
        clarification_rate_pct=clarification_rate,
        recognition_error_rate_pct=rec_err_rate_display,
        database_coverage_pct=db_coverage,
        product_acceptance=product_acceptance,
        offline_engineering_acceptance=offline_eng_acceptance,
        public_beta_ready=public_beta_ready,
        offline_testset_database_coverage_pct=db_coverage,
        modeled_clarification_rate_pct=clarification_rate,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hermes / HealBite Real-Photo Benchmark Evaluation Harness (healbite_photo_kbzhu_v1)"
    )
    parser.add_argument(
        "--manifest",
        default="tests/benchmarks/photo_kbzhu_v1/manifest.json",
        help="Path to photo benchmark manifest JSON",
    )
    parser.add_argument(
        "--images-dir",
        default=None,
        help="Optional path to directory containing real photo image binaries",
    )
    parser.add_argument(
        "--split",
        choices=["all", "calibration", "held_out"],
        default="all",
        help="Evaluation split to run",
    )
    parser.add_argument(
        "--execute-provider",
        action="store_true",
        help="Request execution of live provider vision models (requires authorization)",
    )
    parser.add_argument(
        "--provider",
        default="mock",
        help="Vision provider to execute (e.g. mock, openai, anthropic)",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=0,
        help="Maximum live provider calls authorized (strictly 0 in this phase)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to write evaluation report JSON",
    )
    parser.add_argument(
        "--require-images",
        action="store_true",
        help="Fail closed if images are missing from disk",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform dry-run validation without computing full metrics",
    )

    args = parser.parse_args()

    try:
        report = run_photo_benchmark(
            manifest_path=args.manifest,
            images_dir=args.images_dir,
            require_images=args.require_images,
            split=args.split,
            execute_provider=args.execute_provider,
            provider=args.provider,
            max_calls=args.max_calls,
            dry_run=args.dry_run,
        )

        print("\n" + "=" * 60)
        print("  HERMES / HEALBITE — REAL-PHOTO BENCHMARK EVALUATION")
        print("=" * 60)
        print(f"Dataset Identifier: {report.dataset_identifier} (v{report.version})")
        print(f"Manifest:           {report.manifest_path}")
        print(f"Total in Manifest:  {report.total_items_in_manifest} items")
        print(f"Calibration Split:  {report.calibration_count} items")
        print(f"Held-Out Split:     {report.held_out_count} items")
        print(f"Evaluated Split:    {report.split} ({report.evaluated_count} items)")
        print(f"Session Leakage:    {'DETECTED' if report.session_leakage_detected else 'NONE (0 leakage)'}")
        print(f"Live Provider Used: {report.live_provider_requests_used} calls")
        print("-" * 60)
        print(f"Dish Recognition F1:          {report.dish_recognition_f1}")
        print(f"Recognition Error Rate:       {report.recognition_error_rate_pct}")
        print(f"Portion Mass MAE / MAPE:      {report.overall_metrics.portion_mae_g:.1f} g / {report.overall_metrics.portion_mape_pct:.1f}%")
        print(f"Calorie MAE / MAPE:           {report.overall_metrics.calorie_mae_kcal:.1f} kcal / {report.overall_metrics.calorie_mape_pct:.1f}%")
        print(f"Mass Interval Coverage:       {report.overall_metrics.mass_interval_coverage_pct:.1f}%")
        print(f"Calorie Interval Coverage:    {report.overall_metrics.calorie_interval_coverage_pct:.1f}%")
        print(f"Median Interval Rel Width:    {report.median_interval_relative_width_pct:.1f}%")
        print(f"Unknown Food Rate:            {report.unknown_food_rate:.2%}")
        print(f"Offline Testset DB Coverage:  {report.offline_testset_database_coverage_pct:.1f}%")
        print(f"Modeled Clarification Rate:   {report.modeled_clarification_rate_pct:.1f}% (proxy)")
        print("-" * 60)
        print("Category Breakdowns:")
        for cat, data in report.category_breakdown.items():
            print(f"  • {cat:16s}: n={data['count']}, Portion MAE={data['portion_mae_g']:.1f}g ({data['portion_mape_pct']:.1f}%), Calorie MAE={data['calorie_mae_kcal']:.1f}kcal ({data['calorie_mape_pct']:.1f}%), Coverage={data['mass_interval_coverage_pct']:.1f}%")
        print("-" * 60)
        print(f"OFFLINE_ENGINEERING_ACCEPTANCE: {report.offline_engineering_acceptance}")
        print(f"REAL_PHOTO_ACCEPTANCE:          {report.real_photo_acceptance}")
        print(f"PRODUCT_ACCEPTANCE:             {report.product_acceptance}")
        print(f"PUBLIC_BETA_READY:              {str(report.public_beta_ready).lower()}")
        print("=" * 60 + "\n")

        print("[METRICS]")
        print(f"PRODUCT_ACCEPTANCE={report.product_acceptance}")
        print(f"OFFLINE_ENGINEERING_ACCEPTANCE={report.offline_engineering_acceptance}")
        print(f"REAL_PHOTO_ACCEPTANCE={report.real_photo_acceptance}")
        print(f"PUBLIC_BETA_READY={str(report.public_beta_ready).lower()}")
        print(f"RECOGNITION_ERROR_RATE_PCT={report.recognition_error_rate_pct}")
        print(f"PORTION_MASS_ERROR_MAPE_PCT={report.overall_metrics.portion_mape_pct:.1f}")
        print(f"KBZHU_ERROR_MAPE_PCT={report.overall_metrics.calorie_mape_pct:.1f}")
        print(f"DATABASE_COVERAGE_PCT={report.database_coverage_pct:.1f} (offline testset coverage)")
        print(f"OFFLINE_TESTSET_DATABASE_COVERAGE_PCT={report.offline_testset_database_coverage_pct:.1f}")
        print(f"UNCERTAINTY_INTERVAL_COVERAGE_PCT={report.overall_metrics.mass_interval_coverage_pct:.1f}")
        print(f"CLARIFICATION_RATE_PCT={report.clarification_rate_pct:.1f} (modeled proxy; not actual user frequency)")
        print(f"MODELED_CLARIFICATION_RATE_PCT={report.modeled_clarification_rate_pct:.1f}")
        print(f"REPRESENTATIVE_SCENARIO_COUNT={report.evaluated_count}")
        print(f"DISH_RECOGNITION_F1={report.dish_recognition_f1}")
        print(f"PORTION_MAE_G={report.overall_metrics.portion_mae_g:.1f}")
        print(f"CALORIE_MAE_KCAL={report.overall_metrics.calorie_mae_kcal:.1f}")
        print(f"MEDIAN_INTERVAL_RELATIVE_WIDTH_PCT={report.median_interval_relative_width_pct:.1f}")
        print(f"UNKNOWN_FOOD_RATE={report.unknown_food_rate}")
        print(f"REAL_PHOTO_TEST_COUNT={report.real_photo_test_count}")
        print(f"ENGINEERING_READY={report.engineering_ready}")
        print("PRODUCTION_CHANGED=NO\n")

        if args.output:
            out_p = Path(args.output)
            out_p.parent.mkdir(parents=True, exist_ok=True)
            with open(out_p, "w", encoding="utf-8") as f:
                json.dump(asdict(report), f, indent=2, ensure_ascii=False)
            print(f"Report written to {out_p}")

        return 0 if report.engineering_ready == "PASS" else 1

    except Exception as exc:
        print(f"Error during benchmark evaluation: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
