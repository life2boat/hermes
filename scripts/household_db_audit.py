#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.healbite_household_bootstrap import audit_household_db, print_text, safe_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only HealBite household DB audit")
    parser.add_argument("--db", required=True)
    parser.add_argument("--eligible-users-file")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = audit_household_db(args.db, args.eligible_users_file)
    if args.json:
        print(safe_json(result))
    else:
        print_text(result)
    return 0 if result.get("result") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
