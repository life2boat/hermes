#!/usr/bin/env python3
"""Offline CLI for ProductionRuntimeAttestation v1."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_engineering.production_runtime_attestation import (  # noqa: E402
    ComparisonStatus,
    ObservedRuntimeState,
    ProductionRuntimeAttestation,
    ProductionRuntimeAttestationError,
    compare_production_runtime,
    create_attestation,
    create_collector_result,
    deserialize_attestation,
    deserialize_intended_state,
    normalize_attestation,
    parse_json_document,
    sanitize_json_document,
    serialize_attestation,
    serialize_comparison,
    sign_attestation,
    verify_attestation_signature,
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


def _parse_trusted_keys(trusted_arg: str | None) -> list[str] | None:
    if not trusted_arg:
        return None
    p = Path(trusted_arg)
    if p.is_file():
        text = p.read_text(encoding="utf-8").strip()
        return [k.strip() for k in text.splitlines() if k.strip() and not k.startswith("#")]
    return [k.strip() for k in trusted_arg.split(",") if k.strip()]


def _create(
    input_path: Path,
    output_path: Path,
    private_key_file: Path | None = None,
    signed_at_utc: str | None = None,
) -> int:
    raw = parse_json_document(_read(input_path))
    if not isinstance(raw, dict):
        raise ProductionRuntimeAttestationError("CREATE_INPUT_INVALID")

    # Check if full v1 input
    if "canonical_repository" in raw or "observed_runtime_state" in raw:
        obs_dict = raw.get("observed_runtime_state", {})
        if not isinstance(obs_dict, dict):
            raise ProductionRuntimeAttestationError("CREATE_INPUT_INVALID")
        obs_state = ObservedRuntimeState(
            image_digest=str(obs_dict.get("image_digest", "sha256:" + "0" * 64)),
            oci_revision=str(obs_dict.get("oci_revision", "0" * 40)),
            release_sha=str(obs_dict.get("release_sha", "0" * 40)),
            runtime_contract=str(obs_dict.get("runtime_contract", "hermes-runtime-v1")),
            schema_version=int(obs_dict.get("schema_version", 1)),
            active_services=tuple(sorted(list(obs_dict.get("active_services", ["gateway"])))),
            configuration_contract_version=str(obs_dict.get("configuration_contract_version", "v1")),
            raw_observations=obs_dict.get("raw_observations", {}),
        )
        attestation = create_attestation(
            canonical_repository=str(raw.get("canonical_repository", "https://github.com/life2boat/hermes.git")),
            canonical_main_sha=str(raw.get("canonical_main_sha", "0" * 40)),
            environment_id=str(raw.get("environment_id", raw.get("target", "synthetic-prod"))),
            collection_context_id=str(raw.get("collection_context_id", "default-ctx")),
            observed_at=raw.get("observed_at", raw.get("collected_at_utc")),
            collector_id=str(raw.get("collector_id", "runtime")),
            collector_version=str(raw.get("collector_version", "1.0.0")),
            observed_runtime_state=obs_state,
        )
    else:
        # Legacy input
        if set(raw) != {"target", "collected_at_utc", "collectors"}:
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

    if private_key_file is not None:
        if not private_key_file.is_file() or private_key_file.is_symlink():
            raise ProductionRuntimeAttestationError("PRIVATE_KEY_FILE_INVALID")
        key_str = private_key_file.read_text(encoding="utf-8").strip()
        attestation = sign_attestation(
            attestation, key_str, signed_at_utc=signed_at_utc
        )

    _write_new(output_path, serialize_attestation(attestation))
    return 0


def _verify(
    input_path: Path,
    output_path: Path | None = None,
    trusted_keys: str | None = None,
    integrity_only: bool = False,
    current_time_utc: str | None = None,
    max_age_seconds: int | None = None,
) -> int:
    attestation = deserialize_attestation(_read(input_path))
    if integrity_only:
        if output_path is not None:
            _write_new(output_path, serialize_attestation(attestation))
        return 0

    keys = _parse_trusted_keys(trusted_keys)
    if not keys:
        raise ProductionRuntimeAttestationError("MISSING_TRUST_STORE")
    if attestation.evidence_provenance is None:
        raise ProductionRuntimeAttestationError("MISSING_EVIDENCE_PROVENANCE")

    kwargs: dict[str, object] = {}
    if current_time_utc is not None:
        kwargs["current_time_utc"] = current_time_utc
    if max_age_seconds is not None:
        kwargs["max_evidence_age_seconds"] = max_age_seconds

    valid_sig, reason = verify_attestation_signature(attestation, keys, **kwargs)
    if not valid_sig:
        raise ProductionRuntimeAttestationError(f"SIGNATURE_VERIFICATION_FAILED: {reason}")

    if output_path is not None:
        _write_new(output_path, serialize_attestation(attestation))
    return 0


def _compare(
    intended_path: Path,
    attestation_path: Path,
    output_path: Path,
    trusted_keys: str | None = None,
    current_time_utc: str | None = None,
    max_age_seconds: int | None = None,
) -> int:
    intended = deserialize_intended_state(_read(intended_path))
    attestation = deserialize_attestation(_read(attestation_path))
    keys = _parse_trusted_keys(trusted_keys)

    kwargs: dict[str, object] = {}
    if current_time_utc is not None:
        kwargs["current_time_utc"] = current_time_utc
    if max_age_seconds is not None:
        kwargs["max_evidence_age_seconds"] = max_age_seconds

    comparison = compare_production_runtime(
        intended, attestation, trusted_public_keys=keys, **kwargs
    )
    _write_new(output_path, serialize_comparison(comparison))
    return 0 if comparison.status == ComparisonStatus.MATCH else 2


def _inspect(input_path: Path, output_path: Path | None = None, as_json: bool = False) -> int:
    attestation = deserialize_attestation(_read(input_path))
    data = normalize_attestation(attestation)
    if as_json:
        formatted = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
        if output_path:
            _write_new(output_path, (formatted + "\n").encode("utf-8"))
        else:
            print(formatted)
        return 0

    lines = [
        "============================================================",
        "  PRODUCTION RUNTIME ATTESTATION v1 — INSPECTION",
        "============================================================",
        f"Schema Version:        {attestation.schema_version}",
        f"Attestation ID:        {attestation.attestation_id}",
        f"Target / Environment:  {attestation.target}",
        f"Observed At:           {attestation.observed_at}",
        f"Canonical Repository:  {attestation.canonical_repository}",
        f"Canonical Main SHA:    {attestation.canonical_main_sha}",
        f"Collector ID:          {attestation.collector_id} (v{attestation.collector_version})",
    ]
    if attestation.evidence_provenance:
        lines.extend([
            f"Signer Identity:       {attestation.evidence_provenance.signer_identity}",
            f"Signature Algorithm:   {attestation.evidence_provenance.signature_algorithm}",
            f"Signature Valid Until: {attestation.evidence_provenance.valid_until_utc}",
        ])
    else:
        lines.append("Signature Provenance:  NONE (Unsigned)")

    if attestation.observed_runtime_state:
        obs = attestation.observed_runtime_state
        lines.extend([
            "Observed State:",
            f"  - Image Digest:      {obs.image_digest}",
            f"  - OCI Revision:      {obs.oci_revision}",
            f"  - Release SHA:       {obs.release_sha}",
            f"  - Runtime Contract:  {obs.runtime_contract}",
            f"  - Active Services:   {', '.join(obs.active_services)}",
        ])
    else:
        lines.append(f"Collectors ({len(attestation.collectors)}):")
        for c in attestation.collectors:
            lines.append(f"  - {c.collector_id}: status={c.status.value}, observations={len(c.observations)}")
    lines.append("============================================================")
    report = "\n".join(lines) + "\n"
    if output_path:
        _write_new(output_path, report.encode("utf-8"))
    else:
        print(report, end="")
    return 0


def _sanitize(input_path: Path, output_path: Path) -> int:
    _write_new(output_path, sanitize_json_document(_read(input_path)))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)

    create_cmd = commands.add_parser(
        "create", allow_abbrev=False, help="Create attestation from input JSON"
    )
    create_cmd.add_argument("--input", required=True, type=Path)
    create_cmd.add_argument("--output", required=True, type=Path)
    create_cmd.add_argument(
        "--private-key-file",
        required=False,
        type=Path,
        help="Path to file containing Ed25519 private key hex for signing",
    )
    create_cmd.add_argument(
        "--signed-at-utc",
        required=False,
        type=str,
        help="Optional canonical UTC timestamp for signing",
    )

    verify_cmd = commands.add_parser(
        "verify", allow_abbrev=False, help="Verify attestation integrity and signature"
    )
    verify_cmd.add_argument("--input", required=True, type=Path)
    verify_cmd.add_argument("--output", required=False, type=Path)
    verify_cmd.add_argument("--trusted-keys", required=False, type=str, help="Trusted public key hex or file")
    verify_cmd.add_argument(
        "--integrity-only",
        action="store_true",
        help="Verify structural integrity only without verifying signature",
    )
    verify_cmd.add_argument(
        "--current-time-utc",
        required=False,
        type=str,
        help="Reference UTC time for freshness evaluation",
    )
    verify_cmd.add_argument(
        "--max-age-seconds",
        required=False,
        type=int,
        help="Max evidence age in seconds",
    )

    compare_cmd = commands.add_parser(
        "compare", allow_abbrev=False, help="Compare intended state with attestation"
    )
    compare_cmd.add_argument("--intended", required=True, type=Path)
    compare_cmd.add_argument("--attestation", required=True, type=Path)
    compare_cmd.add_argument("--output", required=True, type=Path)
    compare_cmd.add_argument("--trusted-keys", required=False, type=str, help="Trusted public key hex or file")
    compare_cmd.add_argument(
        "--current-time-utc",
        required=False,
        type=str,
        help="Reference UTC time for freshness evaluation",
    )
    compare_cmd.add_argument(
        "--max-age-seconds",
        required=False,
        type=int,
        help="Max evidence age in seconds",
    )

    inspect_cmd = commands.add_parser(
        "inspect", allow_abbrev=False, help="Inspect attestation structure and provenance"
    )
    inspect_cmd.add_argument("--input", required=True, type=Path)
    inspect_cmd.add_argument("--output", required=False, type=Path, help="Optional output path")
    inspect_cmd.add_argument("--json", action="store_true", help="Output raw JSON")

    sanitize_cmd = commands.add_parser(
        "sanitize", allow_abbrev=False, help="Sanitize JSON evidence document"
    )
    sanitize_cmd.add_argument("--input", required=True, type=Path)
    sanitize_cmd.add_argument("--output", required=True, type=Path)

    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            return _create(
                args.input,
                args.output,
                private_key_file=args.private_key_file,
                signed_at_utc=args.signed_at_utc,
            )
        if args.command == "verify":
            return _verify(
                args.input,
                args.output,
                trusted_keys=args.trusted_keys,
                integrity_only=args.integrity_only,
                current_time_utc=args.current_time_utc,
                max_age_seconds=args.max_age_seconds,
            )
        if args.command == "compare":
            return _compare(
                args.intended,
                args.attestation,
                args.output,
                trusted_keys=args.trusted_keys,
                current_time_utc=args.current_time_utc,
                max_age_seconds=args.max_age_seconds,
            )
        if args.command == "inspect":
            return _inspect(args.input, output_path=args.output, as_json=args.json)
        if args.command == "sanitize":
            return _sanitize(args.input, args.output)
        return 1
    except ProductionRuntimeAttestationError as exc:
        print(f"production_runtime_attestation: {exc.code}", file=sys.stderr)
        return 1
    except OSError:
        print("production_runtime_attestation: FILE_IO_ERROR", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
