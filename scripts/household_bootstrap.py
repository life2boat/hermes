#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.healbite_household_bootstrap import bootstrap_households, print_text, safe_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HealBite household bootstrap tooling")
    parser.add_argument("--db", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--eligible-users-file")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--initialize-schema", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--production-authorization-file")
    args = parser.parse_args(argv)
    result = bootstrap_households(
        args.db,
        apply=bool(args.apply),
        initialize_schema=bool(args.initialize_schema),
        eligible_users_file=args.eligible_users_file,
        batch_size=args.batch_size,
        production_authorization_file=args.production_authorization_file,
    )
    if args.json:
        print(safe_json(result))
    else:
        print_text(result)
    return int(result.get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
