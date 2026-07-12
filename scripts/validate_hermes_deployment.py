#!/usr/bin/env python3
"""Read-only, fail-closed Hermes production deployment validator."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from hermes_deployment_sot import (
    DeploymentSOTError,
    ValidationInputs,
    run_validation,
    safe_failure_detail,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="required read-only mode; no mutation mode exists",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--service-env-file", type=Path, required=True)
    parser.add_argument("--runtime-config", type=Path, required=True)
    parser.add_argument("--secrets-override", type=Path, required=True)
    parser.add_argument("--credential-baseline", type=Path, required=True)
    parser.add_argument("--target-image", required=True)
    parser.add_argument("--rollback-image", required=True)
    parser.add_argument("--expected-qdrant-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.check_only:
        print("CHECK=mode STATUS=FAIL DETAIL=check-only-required")
        return 2

    def absolute_without_resolving(path: Path) -> Path:
        return Path(os.path.abspath(path))

    inputs = ValidationInputs(
        source_root=args.source_root.resolve(),
        manifest_path=absolute_without_resolving(args.manifest),
        expected_sha=args.expected_sha,
        interpolation_env_file=absolute_without_resolving(args.env_file),
        service_env_file=absolute_without_resolving(args.service_env_file),
        runtime_config_file=absolute_without_resolving(args.runtime_config),
        secrets_override_path=absolute_without_resolving(args.secrets_override),
        credential_baseline_path=absolute_without_resolving(args.credential_baseline),
        target_image=args.target_image,
        rollback_image=args.rollback_image,
        expected_qdrant_id=args.expected_qdrant_id,
    )
    try:
        run_validation(inputs)
    except DeploymentSOTError as exc:
        print(f"CHECK=deployment STATUS=FAIL DETAIL={safe_failure_detail(exc)}")
        return 1
    except BaseException:
        print("CHECK=deployment STATUS=FAIL DETAIL=unexpected-validation-failure")
        return 1
    print("CHECK=deployment STATUS=PASS DETAIL=read-only-contract-satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
