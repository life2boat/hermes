"""
gateway.nutrition.manifest — Delivery and runtime binding contracts for the external nutrition catalog.

Normative contract:
- The authoritative nutrition catalog (nutrition_catalog_v2.db) and its manifest
  (nutrition_catalog_v2.manifest.json) are packaged and delivered as an external artifact.
- The manifest declares version, SHA-256 checksum, file size, record count, and source metadata.
- Runtime binding strictly verifies artifact integrity, checksum, and SQLite schema validity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CatalogManifest:
    """Normative manifest contract for nutrition_catalog_v2."""
    catalog_id: str
    format_version: str
    sha256_checksum: str
    file_size_bytes: int
    record_count: int
    sources: list[dict[str, Any]]
    publication_date: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "format_version": self.format_version,
            "sha256_checksum": self.sha256_checksum,
            "file_size_bytes": self.file_size_bytes,
            "record_count": self.record_count,
            "sources": self.sources,
            "publication_date": self.publication_date,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RuntimeCatalogBinding:
    """Runtime status of the bound nutrition catalog database."""
    catalog_path: Path
    manifest_path: Path | None
    manifest: CatalogManifest | None
    is_verified: bool
    verification_error: str | None = None


def compute_file_sha256(path: Path, chunk_size: int = 65536) -> str:
    """Computes SHA-256 checksum of a file in chunks."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def validate_catalog_manifest(data: dict[str, Any]) -> CatalogManifest:
    """
    Validates dictionary structure against the CatalogManifest schema.
    Raises ValueError on missing or invalid fields.
    """
    required_fields = [
        "catalog_id",
        "format_version",
        "sha256_checksum",
        "file_size_bytes",
        "record_count",
        "sources",
        "publication_date",
        "created_at",
    ]
    for req in required_fields:
        if req not in data:
            raise ValueError(f"Catalog manifest missing required field: {req}")

    sha256_val = str(data["sha256_checksum"]).strip().lower()
    if len(sha256_val) != 64 or not all(c in "0123456789abcdef" for c in sha256_val):
        raise ValueError(f"Invalid sha256_checksum in manifest: {sha256_val}")

    file_size = int(data["file_size_bytes"])
    if file_size <= 0:
        raise ValueError(f"file_size_bytes must be positive, got {file_size}")

    record_count = int(data["record_count"])
    if record_count < 0:
        raise ValueError(f"record_count must be non-negative, got {record_count}")

    sources = data.get("sources")
    if not isinstance(sources, list):
        raise ValueError("sources field in manifest must be a list")

    return CatalogManifest(
        catalog_id=str(data["catalog_id"]),
        format_version=str(data["format_version"]),
        sha256_checksum=sha256_val,
        file_size_bytes=file_size,
        record_count=record_count,
        sources=sources,
        publication_date=str(data["publication_date"]),
        created_at=str(data["created_at"]),
    )


def verify_catalog_artifact(catalog_path: Path, manifest: CatalogManifest) -> tuple[bool, str | None]:
    """
    Verifies that the catalog database file exists, matches the expected size,
    and has the exact SHA-256 checksum declared in the manifest.
    """
    if not catalog_path.exists():
        return False, f"Catalog file not found at {catalog_path}"
    if not catalog_path.is_file():
        return False, f"Catalog path is not a regular file: {catalog_path}"

    actual_size = catalog_path.stat().st_size
    if actual_size != manifest.file_size_bytes:
        return False, (
            f"Catalog size mismatch: expected {manifest.file_size_bytes} bytes, "
            f"got {actual_size} bytes"
        )

    actual_sha = compute_file_sha256(catalog_path)
    if actual_sha != manifest.sha256_checksum:
        return False, (
            f"Catalog SHA-256 mismatch: expected {manifest.sha256_checksum}, "
            f"got {actual_sha}"
        )

    return True, None


def verify_catalog_database_schema(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    """
    Performs SQLite integrity check on the catalog database.
    """
    try:
        cursor = conn.execute("PRAGMA integrity_check")
        rows = cursor.fetchall()
        if not rows or rows[0][0] != "ok":
            error_details = ", ".join(r[0] for r in rows)
            return False, f"SQLite integrity check failed: {error_details}"
    except Exception as exc:
        return False, f"SQLite integrity check raised exception: {exc}"

    return True, None


def load_and_verify_catalog_binding(
    catalog_path: Path,
    manifest_path: Path | None = None,
) -> RuntimeCatalogBinding:
    """
    Loads manifest (if provided or colocated) and verifies catalog artifact and schema.
    Returns a RuntimeCatalogBinding record with status.
    """
    catalog_file = Path(catalog_path)

    if manifest_path is None:
        # Check standard colocated manifest filename
        candidate = catalog_file.parent / f"{catalog_file.name}.manifest.json"
        if candidate.exists():
            manifest_path = candidate
        else:
            candidate_alt = catalog_file.with_suffix(".manifest.json")
            if candidate_alt.exists():
                manifest_path = candidate_alt

    if not catalog_file.exists():
        return RuntimeCatalogBinding(
            catalog_path=catalog_file,
            manifest_path=manifest_path,
            manifest=None,
            is_verified=False,
            verification_error=f"Catalog file not found: {catalog_file}",
        )

    manifest: CatalogManifest | None = None
    if manifest_path is None or not manifest_path.exists():
        return RuntimeCatalogBinding(
            catalog_path=catalog_file,
            manifest_path=manifest_path,
            manifest=None,
            is_verified=False,
            verification_error=f"Catalog manifest not found at {manifest_path or 'expected colocated path'}",
        )

    try:
        with manifest_path.open("r", encoding="utf-8") as f:
            manifest_data = json.load(f)
        manifest = validate_catalog_manifest(manifest_data)
    except Exception as exc:
        return RuntimeCatalogBinding(
            catalog_path=catalog_file,
            manifest_path=manifest_path,
            manifest=None,
            is_verified=False,
            verification_error=f"Failed to load or parse manifest: {exc}",
        )

    ok, err = verify_catalog_artifact(catalog_file, manifest)
    if not ok:
        return RuntimeCatalogBinding(
            catalog_path=catalog_file,
            manifest_path=manifest_path,
            manifest=manifest,
            is_verified=False,
            verification_error=err,
        )

    # Verify SQLite integrity
    try:
        conn = sqlite3.connect(f"file:{catalog_file.resolve()}?mode=ro", uri=True)
        try:
            ok, err = verify_catalog_database_schema(conn)
            if not ok:
                return RuntimeCatalogBinding(
                    catalog_path=catalog_file,
                    manifest_path=manifest_path,
                    manifest=manifest,
                    is_verified=False,
                    verification_error=err,
                )
        finally:
            conn.close()
    except Exception as exc:
        return RuntimeCatalogBinding(
            catalog_path=catalog_file,
            manifest_path=manifest_path,
            manifest=manifest,
            is_verified=False,
            verification_error=f"Failed to open catalog database: {exc}",
        )

    return RuntimeCatalogBinding(
        catalog_path=catalog_file,
        manifest_path=manifest_path,
        manifest=manifest,
        is_verified=True,
        verification_error=None,
    )
