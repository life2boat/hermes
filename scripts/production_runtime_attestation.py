#!/usr/bin/env python3
"""Offline CLI for ProductionRuntimeAttestation v1."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_engineering.production_runtime_attestation import (  # noqa: E402
    ComparisonStatus,
    ProductionRuntimeAttestationError,
    compare_production_runtime,
    create_attestation,
    create_collector_result,
    deserialize_attestation,
    deserialize_intended_state,
    parse_json_document,
    sanitize_json_document,
    serialize_attestation,
    serialize_comparison,
)


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ProductionRuntimeAttestationError("INPUT_PATH_INVALID")
    return path.read_bytes()


def _write_new(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir() or path.is_symlink():
        raise ProductionRuntimeAttestationError("OUTPUT_PATH_INVALID")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise ProductionRuntimeAttestationError("OUTPUT_EXISTS") from exc


def _create(input_path: Path, output_path: Path) -> int:
    raw = parse_json_document(_read(input_path))
    if not isinstance(raw, dict) or set(raw) != {
        "target",
        "collected_at_utc",
        "collectors",
    }:
        raise ProductionRuntimeAttestationError("CREATE_INPUT_INVALID")
    collectors_raw = raw["collectors"]
    if not isinstance(collectors_raw, list):
        raise ProductionRuntimeAttestationError("CREATE_INPUT_INVALID")
    collectors = []
    for item in collectors_raw:
        if not isinstance(item, dict) or set(item) != {
            "collector_id",
            "status",
            "observations",
        }:
            raise ProductionRuntimeAttestationError("CREATE_INPUT_INVALID")
        observations = item["observations"]
        if not isinstance(observations, dict):
            raise ProductionRuntimeAttestationError("CREATE_INPUT_INVALID")
        collectors.append(
            create_collector_result(
                item["collector_id"], item["status"], observations
            )
        )
    attestation = create_attestation(
        target=raw["target"],
        collected_at_utc=raw["collected_at_utc"],
        collectors=collectors,
    )
    _write_new(output_path, serialize_attestation(attestation))
    return 0


def _verify(input_path: Path, output_path: Path) -> int:
    attestation = deserialize_attestation(_read(input_path))
    _write_new(output_path, serialize_attestation(attestation))
    return 0


def _compare(intended_path: Path, attestation_path: Path, output_path: Path) -> int:
    intended = deserialize_intended_state(_read(intended_path))
    attestation = deserialize_attestation(_read(attestation_path))
    comparison = compare_production_runtime(intended, attestation)
    _write_new(output_path, serialize_comparison(comparison))
    return 0 if comparison.status == ComparisonStatus.MATCH else 2


def _sanitize(input_path: Path, output_path: Path) -> int:
    _write_new(output_path, sanitize_json_document(_read(input_path)))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "verify", "sanitize"):
        sub = commands.add_parser(command)
        sub.add_argument("--input", required=True, type=Path)
        sub.add_argument("--output", required=True, type=Path)
    compare = commands.add_parser("compare")
    compare.add_argument("--intended", required=True, type=Path)
    compare.add_argument("--attestation", required=True, type=Path)
    compare.add_argument("--output", required=True, type=Path)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            return _create(args.input, args.output)
        if args.command == "verify":
            return _verify(args.input, args.output)
        if args.command == "sanitize":
            return _sanitize(args.input, args.output)
        return _compare(args.intended, args.attestation, args.output)
    except ProductionRuntimeAttestationError as exc:
        print(f"production_runtime_attestation: {exc.code}", file=sys.stderr)
        return 1
    except OSError:
        print("production_runtime_attestation: FILE_IO_ERROR", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
