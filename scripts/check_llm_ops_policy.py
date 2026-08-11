#!/usr/bin/env python3
"""Evaluate the Hermes LLM Ops model and cost policies offline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ai_engineering.contracts import (
    COST_POLICY_VERSION,
    MODEL_POLICY_VERSION,
    AuthorityBoundary,
    EffectClass,
    LLMOpsPolicyError,
    Status,
    StopBoundary,
)
from ai_engineering.cost_policy import (
    aggregate_llm_ops,
    evaluate_cost_policy,
    load_policy_json,
    load_rate_card,
    normalize_cost_policy_receipt,
    normalize_llm_ops_receipt,
)
from ai_engineering.model_policy import (
    evaluate_model_selection,
    normalize_model_policy_receipt,
    normalize_recommendation,
    recommend_model,
)


_MODEL_SELECTION_FIELDS = frozenset(
    {
        "task_class",
        "actual_model",
        "actual_reasoning",
        "substitution_class",
        "substitution_reason_code",
        "substitution_approved",
        "provider_security_change",
        "provider_security_approved",
        "authority_before",
        "authority_after",
        "evidence_refs",
    }
)
_AUTHORITY_FIELDS = frozenset(
    {
        "allowed_effect_classes",
        "forbidden_effect_classes",
        "stop_boundary",
        "production_authorized",
        "secret_access_authorized",
        "data_access_authorized",
    }
)
_COST_FIELDS = frozenset({"budget", "usage", "actual_model", "rate_card_path"})
_COMBINED_FIELDS = frozenset({"model_selection", "cost"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path)
    subparsers = parser.add_subparsers(dest="mode", required=True)

    recommend = subparsers.add_parser("recommend")
    recommend.add_argument("--task-class", required=True)

    for name in ("evaluate-model", "evaluate-cost", "evaluate"):
        command = subparsers.add_parser(name)
        command.add_argument("--input", required=True)
        command.add_argument("--root", type=Path, default=_REPO_ROOT)
    return parser


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise LLMOpsPolicyError("LLM_OPS_INPUT_INVALID")
    return value


def _exact(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    payload = _mapping(value)
    if frozenset(payload) != fields:
        raise LLMOpsPolicyError("LLM_OPS_INPUT_INVALID")
    return payload


def _boolean(value: object, *, optional: bool = False) -> bool | None:
    if value is None and optional:
        return None
    if not isinstance(value, bool):
        raise LLMOpsPolicyError("LLM_OPS_INPUT_INVALID")
    return value


def _string(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > 256:
        raise LLMOpsPolicyError("LLM_OPS_INPUT_INVALID")
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise LLMOpsPolicyError("LLM_OPS_INPUT_INVALID")
    result = tuple(_string(item) for item in value)
    if any(item is None for item in result) or len(result) != len(set(result)):
        raise LLMOpsPolicyError("LLM_OPS_INPUT_INVALID")
    return tuple(item for item in result if item is not None)


def _authority(value: object) -> AuthorityBoundary | None:
    if value is None:
        return None
    payload = _exact(value, _AUTHORITY_FIELDS)
    try:
        allowed = tuple(EffectClass(item) for item in _strings(payload["allowed_effect_classes"]))
        forbidden = tuple(
            EffectClass(item) for item in _strings(payload["forbidden_effect_classes"])
        )
        stop_boundary = StopBoundary(payload["stop_boundary"])
    except (TypeError, ValueError) as exc:
        raise LLMOpsPolicyError("LLM_OPS_INPUT_INVALID") from exc
    if len(allowed) != len(set(allowed)) or len(forbidden) != len(set(forbidden)):
        raise LLMOpsPolicyError("LLM_OPS_INPUT_INVALID")
    return AuthorityBoundary(
        allowed_effect_classes=allowed,
        forbidden_effect_classes=forbidden,
        stop_boundary=stop_boundary,
        production_authorized=bool(_boolean(payload["production_authorized"])),
        secret_access_authorized=bool(
            _boolean(payload["secret_access_authorized"])
        ),
        data_access_authorized=bool(_boolean(payload["data_access_authorized"])),
    )


def _model_receipt(value: object):
    payload = _exact(value, _MODEL_SELECTION_FIELDS)
    return evaluate_model_selection(
        task_class=_string(payload["task_class"]) or "",
        actual_model=_string(payload["actual_model"]) or "",
        actual_reasoning=_string(payload["actual_reasoning"]) or "",
        substitution_class=_string(payload["substitution_class"]) or "",
        substitution_reason_code=_string(
            payload["substitution_reason_code"], optional=True
        ),
        substitution_approved=bool(_boolean(payload["substitution_approved"])),
        provider_security_change=bool(
            _boolean(payload["provider_security_change"])
        ),
        provider_security_approved=_boolean(
            payload["provider_security_approved"], optional=True
        ),
        authority_before=_authority(payload["authority_before"]),
        authority_after=_authority(payload["authority_after"]),
        evidence_refs=_strings(payload["evidence_refs"]),
    )


def _cost_receipt(value: object, root: Path):
    payload = _exact(value, _COST_FIELDS)
    card_path = _string(payload["rate_card_path"], optional=True)
    card = load_rate_card(root, card_path) if card_path is not None else None
    return evaluate_cost_policy(
        budget=_mapping(payload["budget"]),
        usage=_mapping(payload["usage"]),
        actual_model=_string(payload["actual_model"]) or "",
        rate_card=card,
    )


def _root(value: Path) -> Path:
    try:
        root = value.resolve(strict=True)
    except OSError as exc:
        raise LLMOpsPolicyError("LLM_OPS_INPUT_INVALID") from exc
    if not root.is_dir() or root.is_symlink():
        raise LLMOpsPolicyError("LLM_OPS_INPUT_INVALID")
    return root


def _write_new_json(path: Path, payload: dict[str, object]) -> None:
    if path.is_symlink() or path.exists():
        raise LLMOpsPolicyError("LLM_OPS_OUTPUT_UNSAFE")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise LLMOpsPolicyError("LLM_OPS_OUTPUT_UNSAFE") from exc
    if not parent.is_dir():
        raise LLMOpsPolicyError("LLM_OPS_OUTPUT_UNSAFE")
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
    except OSError as exc:
        raise LLMOpsPolicyError("LLM_OPS_OUTPUT_UNSAFE") from exc


def _blocked_payload(code: str, mode: str | None) -> dict[str, object]:
    return {
        "cost_policy_version": COST_POLICY_VERSION,
        "mode": mode,
        "model_policy_version": MODEL_POLICY_VERSION,
        "reason_code": code,
        "status": Status.BLOCKED.value,
    }


def _exit_code(status: Status) -> int:
    return {Status.PASS: 0, Status.FAIL: 1, Status.BLOCKED: 2}[status]


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "recommend":
            payload = normalize_recommendation(recommend_model(args.task_class))
            payload["status"] = Status.PASS.value
            exit_code = 0
        else:
            root = _root(args.root)
            config = load_policy_json(root, args.input)
            if args.mode == "evaluate-model":
                receipt = _model_receipt(config)
                payload = normalize_model_policy_receipt(receipt)
                exit_code = _exit_code(receipt.status)
            elif args.mode == "evaluate-cost":
                receipt = _cost_receipt(config, root)
                payload = normalize_cost_policy_receipt(receipt)
                exit_code = _exit_code(receipt.status)
            else:
                combined = _exact(config, _COMBINED_FIELDS)
                model_receipt = _model_receipt(combined["model_selection"])
                cost_receipt = _cost_receipt(combined["cost"], root)
                receipt = aggregate_llm_ops(model_receipt, cost_receipt)
                payload = normalize_llm_ops_receipt(receipt)
                exit_code = _exit_code(receipt.overall_status)
    except LLMOpsPolicyError as exc:
        payload = _blocked_payload(exc.code, getattr(args, "mode", None))
        exit_code = 2
    except Exception:
        payload = _blocked_payload("LLM_OPS_INTERNAL_ERROR", getattr(args, "mode", None))
        exit_code = 3
    if args.json_out is not None:
        try:
            _write_new_json(args.json_out, payload)
        except LLMOpsPolicyError as exc:
            payload = _blocked_payload(exc.code, getattr(args, "mode", None))
            exit_code = 2
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run())
