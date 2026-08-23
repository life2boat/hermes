#!/usr/bin/env python3
"""Offline CLI for MemoryGraphShadowActivationPreflight verification.

Usage:
    python scripts/check_memory_graph_shadow_readiness.py \
        --subject-main-sha <sha> \
        --expected-subject-main-sha <sha> \
        --candidate-image-revision <rev> \
        --db-path-safe <true|false> \
        --db-integrity <ok|...> \
        --foreign-key-violations <N> \
        --graph-schema-classification <ABSENT|CURRENT|KNOWN_COMPATIBLE_PARTIAL|INCOMPATIBLE> \
        --backup-required <true|false> \
        --backup-valid <true|false> \
        --rollback-proven <true|false> \
        --shadow-mode-available <true|false> \
        --serve-mode-available <true|false> \
        --graph-context-served-to-users <true|false> \
        --production-activation-authorized <true|false> \
        [--output <file>]

All inputs/outputs are offline. No production or network access.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ai_engineering.memory_graph_activation_readiness import (
    check_activation_readiness,
    serialize_preflight,
)

def _parse_bool(val: str) -> bool:
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    raise ValueError(f"Invalid boolean string: {val}")

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--subject-main-sha", required=True)
    parser.add_argument("--expected-subject-main-sha", required=True)
    parser.add_argument("--candidate-image-revision", required=True)
    parser.add_argument("--db-path-safe", required=True, type=_parse_bool)
    parser.add_argument("--db-integrity", required=True)
    parser.add_argument("--foreign-key-violations", required=True, type=int)
    parser.add_argument("--graph-schema-classification", required=True)
    parser.add_argument("--backup-required", required=True, type=_parse_bool)
    parser.add_argument("--backup-valid", required=True, type=_parse_bool)
    parser.add_argument("--rollback-proven", required=True, type=_parse_bool)
    parser.add_argument("--shadow-mode-available", required=True, type=_parse_bool)
    parser.add_argument("--serve-mode-available", required=True, type=_parse_bool)
    parser.add_argument("--graph-context-served-to-users", required=True, type=_parse_bool)
    parser.add_argument("--production-activation-authorized", required=True, type=_parse_bool)
    parser.add_argument("--output", default=None)

    args = parser.parse_args(argv)

    preflight = check_activation_readiness(
        subject_main_sha=args.subject_main_sha,
        candidate_image_revision=args.candidate_image_revision,
        db_path_safe=args.db_path_safe,
        db_integrity=args.db_integrity,
        foreign_key_violations=args.foreign_key_violations,
        graph_schema_classification=args.graph_schema_classification,
        backup_required=args.backup_required,
        rollback_proven=args.rollback_proven,
        shadow_mode_available=args.shadow_mode_available,
        serve_mode_available=args.serve_mode_available,
        graph_context_served_to_users=args.graph_context_served_to_users,
        production_activation_authorized=args.production_activation_authorized,
        expected_subject_main_sha=args.expected_subject_main_sha,
    )

    reason_codes = list(preflight.reason_codes)
    if preflight.backup_required and not args.backup_valid:
        reason_codes.append("BACKUP_INVALID")
        
    verdict = "PASS" if not reason_codes else "BLOCKED"
    
    preflight_json = serialize_preflight(preflight)
    import json
    preflight_dict = json.loads(preflight_json)
    preflight_dict["reason_codes"] = sorted(set(reason_codes))
    preflight_dict["verdict"] = verdict
    preflight_json = json.dumps(preflight_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    if args.output:
        out_path = Path(args.output)
        if out_path.exists():
            print(f"ERROR: output file already exists (create-only): {out_path}", file=sys.stderr)
            return 1
        out_path.write_bytes(preflight_json)
    else:
        sys.stdout.buffer.write(preflight_json)
        sys.stdout.buffer.write(b"\n")

    if verdict == "PASS":
        return 0
    elif verdict == "BLOCKED":
        return 2
    return 1

if __name__ == "__main__":
    sys.exit(main())
