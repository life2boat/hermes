#!/usr/bin/env python3
"""Build the production HealBite Recipe Catalog database deterministically.

Creates an immutable, content-attested SQLite catalog containing the
authorized Escoffier Commons corpus with zero synthetic test fixtures.

Invariants enforced:
- 36 real verified recipes (12 breakfast, 12 lunch, 12 dinner)
- 0 test fixture recipes
- 21-meal weekly plan feasible
- Production canary eligible
- Exact content hash: d31d71658258270f562c444f4469cceceaccf83d309424355fa21db379dd085e
- Atomic replacement of destination file
- Fail-closed validation before replacement
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.healbite_recipe_fixtures import build_escoffier_real_catalog  # noqa: E402

EXPECTED_CONTENT_HASH = "d31d71658258270f562c444f4469cceceaccf83d309424355fa21db379dd085e"
DEFAULT_OUTPUT_PATH = Path("/var/lib/hermes/recipe-catalog/recipe_catalog.db")
DEFAULT_RECIPES_JSON = REPO_ROOT / "recipe_corpus" / "authorized_inputs" / "escoffier_recipes.json"


def build_production_catalog(
    output_path: Path,
    *,
    recipes_json: Path = DEFAULT_RECIPES_JSON,
    reports_dir: Path | None = None,
    expected_hash: str = EXPECTED_CONTENT_HASH,
    target_rights_scope: str = "FR,US",
) -> str:
    output_path = output_path.resolve()
    target_dir = output_path.parent
    target_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=target_dir, prefix=".recipe-catalog-build-") as tmpdir:
        temp_db = Path(tmpdir) / "catalog_stage.db"
        content_hash, readiness = build_escoffier_real_catalog(
            temp_db,
            build_id="escoffier-commons-v1",
            reports_dir=reports_dir,
            recipes_json_path=recipes_json,
            include_test_fixtures=False,
            target_rights_scope=tuple(s.strip() for s in target_rights_scope.split(",") if s.strip()),
        )

        if readiness.real_verified_recipes != 36:
            raise RuntimeError(
                f"Unexpected real verified recipe count: {readiness.real_verified_recipes} (expected 36)"
            )
        if readiness.test_fixture_recipes != 0:
            raise RuntimeError(
                f"Production catalog must not contain test fixture recipes (found {readiness.test_fixture_recipes})"
            )
        if (
            readiness.breakfast_verified != 12
            or readiness.lunch_verified != 12
            or readiness.dinner_verified != 12
        ):
            raise RuntimeError(
                f"Meal distribution mismatch: {readiness.breakfast_verified}/{readiness.lunch_verified}/{readiness.dinner_verified} (expected 12/12/12)"
            )
        if not readiness.can_form_21_meal_week:
            raise RuntimeError("Catalog cannot form a 21-meal weekly plan")
        if not readiness.production_canary_eligible:
            raise RuntimeError("Catalog is not production canary eligible")
        if content_hash != expected_hash:
            raise RuntimeError(
                f"Catalog content hash mismatch: computed={content_hash} expected={expected_hash}"
            )

        # Set read-only permissions (0o644)
        temp_db.chmod(0o644)

        # Atomically replace destination
        temp_db.replace(output_path)

    return content_hash


def main() -> None:
    parser = argparse.ArgumentParser(description="Build production HealBite Recipe Catalog")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Path where production catalog DB will be written",
    )
    parser.add_argument(
        "--recipes-json",
        type=Path,
        default=DEFAULT_RECIPES_JSON,
        help="Path to authorized recipes JSON file",
    )
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=None,
        help="Optional directory to write build and coverage reports",
    )
    parser.add_argument(
        "--target-rights-scope",
        type=str,
        default="FR,US",
        help="Target rights scope jurisdictions (default: FR,US)",
    )
    args = parser.parse_args()

    try:
        content_hash = build_production_catalog(
            args.output,
            recipes_json=args.recipes_json,
            reports_dir=args.reports_dir,
            target_rights_scope=args.target_rights_scope,
        )
        print("CATALOG_BUILD=PASS")
        print(f"CATALOG_OUTPUT={args.output}")
        print(f"CATALOG_CONTENT_HASH={content_hash}")
        print("RECIPES_COUNT=36")
        print("MEAL_DISTRIBUTION=12/12/12")
        print("FIXTURES_COUNT=0")
    except Exception as exc:
        print("CATALOG_BUILD=FAIL", file=sys.stderr)
        print(f"ERROR={exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
