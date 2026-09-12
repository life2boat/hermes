from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCORER_V1_LEGACY = "scorer_v1_legacy"
SCORER_V2_EXPLICIT_ALIAS_COMPOUND = "scorer_v2_explicit_alias_compound"
QUALITY_THRESHOLDS = {
    "major_component_precision": 0.90,
    "major_component_recall": 0.90,
    "sauce_recall": 0.90,
    "confirmation_correctness": 1.0,
    "aggregate_nutrition_violations": 0,
    "invalid_staging": 0,
}
_V2_PUNCTUATION_RE = re.compile(r"[_/\-|,;:()\[\]{}\\]+")


@dataclass(frozen=True)
class RecognizedItem:
    visible_name: str
    normalized_name: str
    is_sauce: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecognizedItem":
        return cls(
            visible_name=str(data.get("visible_name") or ""),
            normalized_name=str(data.get("normalized_name") or ""),
            is_sauce=bool(data.get("is_sauce")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "visible_name": self.visible_name,
            "normalized_name": self.normalized_name,
            "is_sauce": self.is_sauce,
        }


def normalize_label_v1(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def normalize_label_v2(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold().strip()
    normalized = _V2_PUNCTUATION_RE.sub(" ", normalized)
    return " ".join(normalized.split())


def load_scorer_v2_contract(path: str | Path | None = None) -> dict[str, Any]:
    contract_path = Path(path) if path is not None else Path(__file__).with_name("healbite_food_vision_benchmark_contract_v2.json")
    data = json.loads(contract_path.read_text(encoding="utf-8"))
    if data.get("scorer_version") != SCORER_V2_EXPLICIT_ALIAS_COMPOUND:
        msg = f"unexpected scorer_version in contract: {data.get('scorer_version')!r}"
        raise ValueError(msg)
    return data


def manifest_image_map(manifest_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in manifest_data.get("images", [])}


def load_manifest_image_map(path: str | Path) -> dict[str, dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return manifest_image_map(data)


def load_historical_responses(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data.get("responses"), list):
        raise ValueError(f"{path} does not contain a responses list")
    return data


def _normalizer_for(scorer_version: str):
    if scorer_version == SCORER_V1_LEGACY:
        return normalize_label_v1
    if scorer_version == SCORER_V2_EXPLICIT_ALIAS_COMPOUND:
        return normalize_label_v2
    raise ValueError(f"unsupported scorer version: {scorer_version}")


def _canonical_expected_groups(
    expected_alias_groups: list[list[str]],
    scorer_version: str,
) -> list[dict[str, Any]]:
    normalize = _normalizer_for(scorer_version)
    groups: list[dict[str, Any]] = []
    for aliases in expected_alias_groups:
        normalized_aliases = [normalize(alias) for alias in aliases if normalize(alias)]
        if not normalized_aliases:
            continue
        groups.append(
            {
                "canonical": normalized_aliases[0],
                "aliases": normalized_aliases,
            }
        )
    return groups


def _recognized_candidates(item: RecognizedItem, scorer_version: str) -> list[str]:
    normalize = _normalizer_for(scorer_version)
    ordered = [normalize(item.visible_name), normalize(item.normalized_name)]
    deduped: list[str] = []
    for candidate in ordered:
        if candidate and candidate not in deduped:
            deduped.append(candidate)
    return deduped


def _index_v2_contract(
    contract: dict[str, Any],
    expected_canonicals: list[str],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    expected_set = set(expected_canonicals)
    single_aliases: dict[str, str] = {}
    for entry in contract.get("single_component_aliases", []):
        canonical = normalize_label_v2(str(entry.get("canonical_component") or ""))
        alias = normalize_label_v2(str(entry.get("alias") or ""))
        if alias and canonical in expected_set:
            single_aliases[alias] = canonical
    compound_aliases: dict[str, list[str]] = {}
    for entry in contract.get("compound_mappings", []):
        phrase = normalize_label_v2(str(entry.get("phrase") or ""))
        components = [normalize_label_v2(str(value or "")) for value in entry.get("canonical_components", [])]
        filtered = [value for value in components if value in expected_set]
        if phrase and filtered:
            compound_aliases[phrase] = filtered
    return single_aliases, compound_aliases


def score_food_components(
    *,
    scorer_version: str,
    expected_major_components: list[list[str]],
    expected_sauces: list[list[str]],
    recognized_items: list[dict[str, Any]] | list[RecognizedItem],
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = [item if isinstance(item, RecognizedItem) else RecognizedItem.from_dict(item) for item in recognized_items]
    expected_major_groups = _canonical_expected_groups(expected_major_components, scorer_version)
    expected_sauce_groups = _canonical_expected_groups(expected_sauces, scorer_version)
    expected_major_canonicals = [group["canonical"] for group in expected_major_groups]
    expected_sauce_canonicals = [group["canonical"] for group in expected_sauce_groups]
    validation_errors: list[str] = []

    if scorer_version == SCORER_V2_EXPLICIT_ALIAS_COMPOUND and contract is None:
        contract = load_scorer_v2_contract()

    matched_predictions: list[dict[str, Any]] = []
    unmatched_predictions: list[dict[str, Any]] = []
    compound_mappings_used: list[dict[str, Any]] = []
    normalized_major_names: list[list[str]] = []
    normalized_sauce_names: list[list[str]] = []

    covered_major: set[str] = set()
    covered_sauces: set[str] = set()
    matched_major_prediction_indexes: set[int] = set()
    matched_sauce_prediction_indexes: set[int] = set()

    major_alias_index = {
        alias: group["canonical"]
        for group in expected_major_groups
        for alias in group["aliases"]
    }
    sauce_alias_index = {
        alias: group["canonical"]
        for group in expected_sauce_groups
        for alias in group["aliases"]
    }
    v2_single_major: dict[str, str] = {}
    v2_compound_major: dict[str, list[str]] = {}
    if scorer_version == SCORER_V2_EXPLICIT_ALIAS_COMPOUND:
        v2_single_major, v2_compound_major = _index_v2_contract(contract or {}, expected_major_canonicals)

    for index, item in enumerate(items):
        candidates = _recognized_candidates(item, scorer_version)
        if item.is_sauce:
            normalized_sauce_names.append(candidates)
        else:
            normalized_major_names.append(candidates)
        if not candidates:
            unmatched_predictions.append(
                {
                    "recognized_index": index,
                    "visible_name": item.visible_name,
                    "normalized_name": item.normalized_name,
                    "is_sauce": item.is_sauce,
                    "reason": "empty_candidates",
                }
            )
            continue

        if item.is_sauce:
            matched = False
            for candidate in candidates:
                canonical = sauce_alias_index.get(candidate)
                if canonical is None or canonical in covered_sauces:
                    continue
                covered_sauces.add(canonical)
                matched_sauce_prediction_indexes.add(index)
                matched_predictions.append(
                    {
                        "recognized_index": index,
                        "visible_name": item.visible_name,
                        "normalized_name": item.normalized_name,
                        "is_sauce": True,
                        "covered_expected_components": [canonical],
                        "match_type": "direct_alias",
                    }
                )
                matched = True
                break
            if not matched:
                unmatched_predictions.append(
                    {
                        "recognized_index": index,
                        "visible_name": item.visible_name,
                        "normalized_name": item.normalized_name,
                        "is_sauce": True,
                        "reason": "no_expected_match",
                    }
                )
            continue

        compound_choice: tuple[str, list[str]] | None = None
        if scorer_version == SCORER_V2_EXPLICIT_ALIAS_COMPOUND:
            for candidate in candidates:
                mapped = v2_compound_major.get(candidate)
                if not mapped:
                    continue
                uncovered = [canonical for canonical in mapped if canonical not in covered_major]
                if not uncovered:
                    continue
                compound_choice = (candidate, uncovered)
                break

        if compound_choice is not None:
            phrase, uncovered = compound_choice
            covered_major.update(uncovered)
            matched_major_prediction_indexes.add(index)
            matched_predictions.append(
                {
                    "recognized_index": index,
                    "visible_name": item.visible_name,
                    "normalized_name": item.normalized_name,
                    "is_sauce": False,
                    "covered_expected_components": uncovered,
                    "match_type": "explicit_compound",
                }
            )
            compound_mappings_used.append(
                {
                    "recognized_index": index,
                    "phrase": phrase,
                    "covered_expected_components": uncovered,
                }
            )
            continue

        ordinary_matches: list[str] = []
        for candidate in candidates:
            canonical = major_alias_index.get(candidate)
            if canonical is not None:
                ordinary_matches.append(canonical)
            if scorer_version == SCORER_V2_EXPLICIT_ALIAS_COMPOUND:
                alias_canonical = v2_single_major.get(candidate)
                if alias_canonical is not None:
                    ordinary_matches.append(alias_canonical)
        ordinary_matches = [canonical for canonical in expected_major_canonicals if canonical in ordinary_matches]

        chosen: str | None = None
        for canonical in ordinary_matches:
            if canonical not in covered_major:
                chosen = canonical
                break
        if chosen is not None:
            covered_major.add(chosen)
            matched_major_prediction_indexes.add(index)
            match_type = "direct_alias"
            if any(v2_single_major.get(candidate) == chosen for candidate in candidates):
                match_type = "explicit_single_alias"
            matched_predictions.append(
                {
                    "recognized_index": index,
                    "visible_name": item.visible_name,
                    "normalized_name": item.normalized_name,
                    "is_sauce": False,
                    "covered_expected_components": [chosen],
                    "match_type": match_type,
                }
            )
            continue

        unmatched_predictions.append(
            {
                "recognized_index": index,
                "visible_name": item.visible_name,
                "normalized_name": item.normalized_name,
                "is_sauce": False,
                "reason": "no_expected_match_or_already_covered",
            }
        )

    recognized_major_count = sum(1 for item in items if not item.is_sauce)
    recognized_sauce_count = sum(1 for item in items if item.is_sauce)
    major_precision_numerator = len(matched_major_prediction_indexes)
    major_precision_denominator = recognized_major_count
    major_recall_numerator = len(covered_major)
    major_recall_denominator = len(expected_major_canonicals)
    sauce_recall_numerator = len(covered_sauces)
    sauce_recall_denominator = len(expected_sauce_canonicals)

    major_precision = major_precision_numerator / major_precision_denominator if major_precision_denominator else 0.0
    major_recall = major_recall_numerator / major_recall_denominator if major_recall_denominator else 1.0
    sauce_recall = sauce_recall_numerator / sauce_recall_denominator if sauce_recall_denominator else 1.0

    return {
        "scorer_version": scorer_version,
        "contract_version": contract.get("contract_version") if contract else None,
        "normalized_recognized_major_names": normalized_major_names,
        "normalized_recognized_sauce_names": normalized_sauce_names,
        "matched_recognized_predictions": matched_predictions,
        "covered_expected_components": sorted(covered_major),
        "unmatched_recognized_components": unmatched_predictions,
        "uncovered_expected_components": [canonical for canonical in expected_major_canonicals if canonical not in covered_major],
        "compound_mappings_used": compound_mappings_used,
        "major_true_positive_prediction_count": major_precision_numerator,
        "major_false_positive_prediction_count": max(0, major_precision_denominator - major_precision_numerator),
        "major_false_negative_expected_count": max(0, major_recall_denominator - major_recall_numerator),
        "major_precision_numerator": major_precision_numerator,
        "major_precision_denominator": major_precision_denominator,
        "major_recall_numerator": major_recall_numerator,
        "major_recall_denominator": major_recall_denominator,
        "major_precision": round(major_precision, 6),
        "major_recall": round(major_recall, 6),
        "sauce_recall_numerator": sauce_recall_numerator,
        "sauce_recall_denominator": sauce_recall_denominator,
        "sauce_recall": round(sauce_recall, 6),
        "recognized_major_component_count": recognized_major_count,
        "recognized_sauce_count": recognized_sauce_count,
        "validation_errors": validation_errors,
    }


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _asset_sanitized_replay(
    *,
    asset_id: str,
    asset_contract: dict[str, Any],
    response: dict[str, Any],
    scorer_version: str,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    replay = score_food_components(
        scorer_version=scorer_version,
        expected_major_components=list(asset_contract.get("expected_major_components", [])),
        expected_sauces=list(asset_contract.get("expected_sauces", [])),
        recognized_items=list(response.get("recognized_items", [])),
        contract=contract,
    )
    replay.update(
        {
            "asset_id": asset_id,
            "historical_major_component_precision": response.get("major_component_precision"),
            "historical_major_component_recall": response.get("major_component_recall"),
            "historical_sauce_recall": response.get("sauce_recall"),
            "historical_confirmation": response.get(
                "final_confirmation_requirement",
                response.get("provider_confirmation_value"),
            ),
            "historical_validation_status": response.get("validation_status"),
            "replayable": isinstance(response.get("recognized_items"), list),
        }
    )
    return replay


def _aggregate_quality(replays: list[dict[str, Any]], manifest_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    total_precision_num = 0
    total_precision_den = 0
    total_recall_num = 0
    total_recall_den = 0
    total_sauce_num = 0
    total_sauce_den = 0
    for replay in replays:
        asset = manifest_map[replay["asset_id"]]
        if asset.get("type") == "ambiguous" or not asset.get("expected_major_components"):
            continue
        total_precision_num += int(replay["major_precision_numerator"])
        total_precision_den += int(replay["major_precision_denominator"])
        total_recall_num += int(replay["major_recall_numerator"])
        total_recall_den += int(replay["major_recall_denominator"])
        total_sauce_num += int(replay["sauce_recall_numerator"])
        total_sauce_den += int(replay["sauce_recall_denominator"])
    major_precision = total_precision_num / total_precision_den if total_precision_den else 0.0
    major_recall = total_recall_num / total_recall_den if total_recall_den else 1.0
    sauce_recall = total_sauce_num / total_sauce_den if total_sauce_den else 1.0
    return {
        "major_component_precision": round(major_precision, 6),
        "major_component_recall": round(major_recall, 6),
        "sauce_recall": round(sauce_recall, 6),
        "major_precision_numerator": total_precision_num,
        "major_precision_denominator": total_precision_den,
        "major_recall_numerator": total_recall_num,
        "major_recall_denominator": total_recall_den,
    }


def replay_historical_model(
    *,
    model_id: str,
    historical_data: dict[str, Any],
    manifest_map: dict[str, dict[str, Any]],
    scorer_version: str,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    responses = historical_data.get("responses", [])
    by_asset = {response["asset_id"]: response for response in responses if response.get("asset_id") in manifest_map}
    replayable_assets: list[str] = []
    missing_assets: list[str] = []
    per_asset: list[dict[str, Any]] = []
    for asset_id, asset_contract in manifest_map.items():
        response = by_asset.get(asset_id)
        if response is None or not isinstance(response.get("recognized_items"), list):
            missing_assets.append(asset_id)
            continue
        replayable_assets.append(asset_id)
        per_asset.append(
            _asset_sanitized_replay(
                asset_id=asset_id,
                asset_contract=asset_contract,
                response=response,
                scorer_version=scorer_version,
                contract=contract,
            )
        )

    aggregate = _aggregate_quality(per_asset, manifest_map)
    confirmation_correctness = historical_data.get("confirmation_correctness")
    ambiguity_result = historical_data.get("ambiguity_gate", historical_data.get("ambiguity_result"))
    historical_safety_gates_valid = (
        historical_data.get("aggregate_nutrition_violations") == 0
        and historical_data.get("invalid_staging") == 0
        and float(confirmation_correctness) == QUALITY_THRESHOLDS["confirmation_correctness"]
        and _boolish(ambiguity_result)
    )
    exact_v1_reproduction = True
    if scorer_version == SCORER_V1_LEGACY:
        for replay in per_asset:
            if round(float(replay["historical_major_component_precision"]), 6) != round(float(replay["major_precision"]), 6):
                exact_v1_reproduction = False
            if round(float(replay["historical_major_component_recall"]), 6) != round(float(replay["major_recall"]), 6):
                exact_v1_reproduction = False
            if round(float(replay["historical_sauce_recall"]), 6) != round(float(replay["sauce_recall"]), 6):
                exact_v1_reproduction = False
    replay_complete = len(replayable_assets) == len(manifest_map) and not missing_assets

    gate_result = "SCORER_V2_REPLAY_INCOMPLETE"
    if scorer_version == SCORER_V2_EXPLICIT_ALIAS_COMPOUND:
        if replay_complete:
            v1_check = replay_historical_model(
                model_id=model_id,
                historical_data=historical_data,
                manifest_map=manifest_map,
                scorer_version=SCORER_V1_LEGACY,
                contract=None,
            )
            if (
                v1_check["exact_v1_reproduction"]
                and historical_safety_gates_valid
                and aggregate["major_component_precision"] >= QUALITY_THRESHOLDS["major_component_precision"]
                and aggregate["major_component_recall"] >= QUALITY_THRESHOLDS["major_component_recall"]
                and aggregate["sauce_recall"] >= QUALITY_THRESHOLDS["sauce_recall"]
            ):
                gate_result = "SCORER_V2_REPLAY_GATE_PASS"
            else:
                gate_result = "SCORER_V2_REPLAY_GATE_FAIL"

    return {
        "model_id": model_id,
        "scorer_version": scorer_version,
        "contract_version": contract.get("contract_version") if contract else None,
        "assets_replayable": replayable_assets,
        "assets_not_replayable": missing_assets,
        "historical_replay_complete": replay_complete,
        "exact_v1_reproduction": exact_v1_reproduction,
        "ambiguity_result": ambiguity_result,
        "confirmation_correctness": confirmation_correctness,
        "historical_safety_gates_valid": historical_safety_gates_valid,
        "quality_gate_result": gate_result,
        "aggregate_metrics": aggregate,
        "per_asset": per_asset,
    }


def render_model_comparison_markdown(legacy: dict[str, Any], scorer_v2: dict[str, Any]) -> str:
    lines = [
        "# Canonical Food-Vision Benchmark Replay",
        "",
    ]
    for model_id in legacy:
        v1 = legacy[model_id]
        v2 = scorer_v2[model_id]
        lines.extend(
            [
                f"## {model_id}",
                "",
                f"- v1 exact reproduction: {str(v1['exact_v1_reproduction']).lower()}",
                f"- replay complete: {str(v2['historical_replay_complete']).lower()}",
                f"- v2 gate: {v2['quality_gate_result']}",
                f"- v2 major precision: {v2['aggregate_metrics']['major_component_precision']}",
                f"- v2 major recall: {v2['aggregate_metrics']['major_component_recall']}",
                f"- v2 sauce recall: {v2['aggregate_metrics']['sauce_recall']}",
                "",
            ]
        )
    return "\n".join(lines) + "\n"
