#!/usr/bin/env python3
"""
scripts/build_nutrition_catalog.py — Build the authoritative nutrition_catalog_v2.db artifact and manifest.

Usage:
    python scripts/build_nutrition_catalog.py [--output-dir DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to path
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gateway.nutrition.etl.catalog_builder import build_nutrition_catalog
from gateway.nutrition.manifest import load_and_verify_catalog_binding


def main() -> int:
    parser = argparse.ArgumentParser(description="Build HealBite Nutrition Catalog v2")
    parser.add_argument("--output-dir", type=str, default=".", help="Output directory for db and manifest")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    db_path = out_dir / "nutrition_catalog_v2.db"
    manifest_path = out_dir / "nutrition_catalog_v2.manifest.json"

    print(f"Building nutrition catalog v2 at {db_path}...")
    catalog, manifest = build_nutrition_catalog(
        output_db_path=db_path,
        output_manifest_path=manifest_path,
    )

    print(f"Built catalog with {manifest.record_count} food records.")
    print(f"Catalog SHA-256: {manifest.sha256_checksum}")
    print(f"Catalog Size: {manifest.file_size_bytes} bytes")
    print(f"Manifest written to: {manifest_path}")

    # Verify runtime binding
    binding = load_and_verify_catalog_binding(db_path, manifest_path)
    if not binding.is_verified:
        print(f"ERROR: Runtime catalog binding verification failed: {binding.verification_error}", file=sys.stderr)
        return 1

    print("Runtime catalog binding successfully verified (PASS).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
