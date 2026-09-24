from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_deploy_preflight as preflight
import hermes_post_deploy_attestation as attestation
import hermes_production_deploy as deploy
from build_production_recipe_catalog import (
    build_production_catalog,
    EXPECTED_CONTENT_HASH,
)
from test_hermes_production_deploy import protected_contract

REVISION = "9e7b6a67890db6b6acbe94f2bb7fba35f1426a7f"
IMAGE_ID = "sha256:" + "a" * 64
PREVIOUS_IMAGE_ID = "sha256:" + "b" * 64
RECIPE_FEATURE = "HEALBITE_RECIPE_GROUNDED_MENU"
RECIPE_ENABLED = f"{RECIPE_FEATURE}_ENABLED"
RECIPE_ALLOWLIST = f"{RECIPE_FEATURE}_ALLOWLIST"


# ============================================================
# 1. CANONICAL CONTRACT DECLARATION & MANIFEST POLICY
# ============================================================


def test_manifest_declares_canonical_recipe_catalog_mount() -> None:
    contract = deploy.load_contract(REPO_ROOT)
    assert contract.recipe_catalog_source == Path(
        "/var/lib/hermes/recipe-catalog/recipe_catalog.db"
    )
    assert contract.recipe_catalog_target == Path("/home/hermes/recipe_catalog.db")
    assert contract.recipe_catalog_mount_type == "bind"
    assert contract.recipe_catalog_read_only is True

    assert (
        contract.runtime_bindings["HEALBITE_RECIPE_CATALOG_PATH"]
        == "/home/hermes/recipe_catalog.db"
    )
    assert contract.public_gates["HEALBITE_RECIPE_GROUNDED_MENU_PUBLIC"] == "false"
    assert contract.feature_gates[RECIPE_ENABLED] == "false"
    assert contract.feature_gates[RECIPE_ALLOWLIST] == ""
    assert RECIPE_FEATURE in contract.canary_authorized_features


def test_manifest_rejects_missing_recipe_catalog_mount() -> None:
    raw = json.loads(
        (REPO_ROOT / "deploy" / "hermes-production.json").read_text(encoding="utf-8")
    )
    del raw["recipe_catalog_mount"]
    with pytest.raises(deploy.DeploymentContractError, match="manifest-fields"):
        deploy.load_contract(REPO_ROOT, manifest_bytes=json.dumps(raw).encode("utf-8"))


def test_manifest_rejects_writable_recipe_catalog_mount() -> None:
    raw = json.loads(
        (REPO_ROOT / "deploy" / "hermes-production.json").read_text(encoding="utf-8")
    )
    raw["recipe_catalog_mount"]["read_write"] = True
    with pytest.raises(
        deploy.DeploymentContractError, match="recipe-catalog-mount-policy"
    ):
        deploy.load_contract(REPO_ROOT, manifest_bytes=json.dumps(raw).encode("utf-8"))


def test_manifest_rejects_wrong_type_recipe_catalog_mount() -> None:
    raw = json.loads(
        (REPO_ROOT / "deploy" / "hermes-production.json").read_text(encoding="utf-8")
    )
    raw["recipe_catalog_mount"]["type"] = "volume"
    with pytest.raises(
        deploy.DeploymentContractError, match="recipe-catalog-mount-policy"
    ):
        deploy.load_contract(REPO_ROOT, manifest_bytes=json.dumps(raw).encode("utf-8"))


# ============================================================
# 2. COMPOSE PREFLIGHT MOUNT VALIDATION
# ============================================================


def test_compose_preflight_mount_validation_passes() -> None:
    mount = preflight.MountRecord(
        source="/var/lib/hermes/recipe-catalog/recipe_catalog.db",
        target="/home/hermes/recipe_catalog.db",
        mount_type="bind",
        read_only=True,
    )
    result = preflight.validate_recipe_catalog_mounts(
        [mount],
        expected_source="/var/lib/hermes/recipe-catalog/recipe_catalog.db",
        expected_target="/home/hermes/recipe_catalog.db",
        expected_type="bind",
        expected_read_only=True,
    )
    assert result == mount


def test_compose_preflight_mount_validation_fails_on_missing() -> None:
    with pytest.raises(
        preflight.DeployPreflightError, match="missing-canonical-recipe-catalog-mount"
    ):
        preflight.validate_recipe_catalog_mounts(
            [],
            expected_source="/var/lib/hermes/recipe-catalog/recipe_catalog.db",
            expected_target="/home/hermes/recipe_catalog.db",
            expected_type="bind",
            expected_read_only=True,
        )


def test_compose_preflight_mount_validation_fails_on_wrong_source() -> None:
    mount = preflight.MountRecord(
        source="/var/lib/hermes/other/recipe_catalog.db",
        target="/home/hermes/recipe_catalog.db",
        mount_type="bind",
        read_only=True,
    )
    with pytest.raises(
        preflight.DeployPreflightError, match="wrong-recipe-catalog-source"
    ):
        preflight.validate_recipe_catalog_mounts(
            [mount],
            expected_source="/var/lib/hermes/recipe-catalog/recipe_catalog.db",
            expected_target="/home/hermes/recipe_catalog.db",
            expected_type="bind",
            expected_read_only=True,
        )


def test_compose_preflight_mount_validation_fails_on_wrong_target() -> None:
    mount = preflight.MountRecord(
        source="/var/lib/hermes/recipe-catalog/recipe_catalog.db",
        target="/home/hermes/other_catalog.db",
        mount_type="bind",
        read_only=True,
    )
    with pytest.raises(
        preflight.DeployPreflightError, match="wrong-recipe-catalog-target"
    ):
        preflight.validate_recipe_catalog_mounts(
            [mount],
            expected_source="/var/lib/hermes/recipe-catalog/recipe_catalog.db",
            expected_target="/home/hermes/recipe_catalog.db",
            expected_type="bind",
            expected_read_only=True,
        )


def test_compose_preflight_mount_validation_fails_on_writable() -> None:
    mount = preflight.MountRecord(
        source="/var/lib/hermes/recipe-catalog/recipe_catalog.db",
        target="/home/hermes/recipe_catalog.db",
        mount_type="bind",
        read_only=False,  # RW
    )
    with pytest.raises(
        preflight.DeployPreflightError, match="wrong-recipe-catalog-mount-mode"
    ):
        preflight.validate_recipe_catalog_mounts(
            [mount],
            expected_source="/var/lib/hermes/recipe-catalog/recipe_catalog.db",
            expected_target="/home/hermes/recipe_catalog.db",
            expected_type="bind",
            expected_read_only=True,
        )


def test_compose_preflight_mount_validation_fails_on_duplicate_target() -> None:
    m1 = preflight.MountRecord(
        source="/var/lib/hermes/recipe-catalog/recipe_catalog.db",
        target="/home/hermes/recipe_catalog.db",
        mount_type="bind",
        read_only=True,
    )
    m2 = preflight.MountRecord(
        source="/var/lib/hermes/recipe-catalog/other.db",
        target="/home/hermes/recipe_catalog.db",
        mount_type="bind",
        read_only=True,
    )
    with pytest.raises(
        preflight.DeployPreflightError, match="duplicate-recipe-catalog-target"
    ):
        preflight.validate_recipe_catalog_mounts(
            [m1, m2],
            expected_source="/var/lib/hermes/recipe-catalog/recipe_catalog.db",
            expected_target="/home/hermes/recipe_catalog.db",
            expected_type="bind",
            expected_read_only=True,
        )


# ============================================================
# 3. RECIPE CATALOG SOURCE PATH VALIDATION
# ============================================================


def test_recipe_catalog_source_path_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nonexistent.db"
    with pytest.raises(preflight.DeployPreflightError, match="recipe-catalog-missing"):
        preflight.validate_recipe_catalog_source_path(missing)


def test_recipe_catalog_source_path_directory(tmp_path: Path) -> None:
    dir_path = tmp_path / "catalog_dir"
    dir_path.mkdir()
    with pytest.raises(
        preflight.DeployPreflightError, match="unsafe-recipe-catalog-file"
    ):
        preflight.validate_recipe_catalog_source_path(dir_path)


def test_recipe_catalog_source_path_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real.db"
    real.write_bytes(b"dummy")
    sym = tmp_path / "symlink.db"
    try:
        sym.symlink_to(real)
    except OSError:
        pytest.skip("Symlink not supported in environment")
    with pytest.raises(preflight.DeployPreflightError, match="symlink-path"):
        preflight.validate_recipe_catalog_source_path(sym)


def test_recipe_catalog_source_path_insecure_mode(tmp_path: Path) -> None:
    catalog = tmp_path / "insecure.db"
    catalog.write_bytes(b"dummy")
    try:
        catalog.chmod(0o666)  # world-writable
    except OSError:
        pytest.skip("Chmod not supported")
    current_mode = stat.S_IMODE(catalog.stat().st_mode)
    if current_mode & 0o002:
        with pytest.raises(
            preflight.DeployPreflightError, match="unsafe-recipe-catalog-permissions"
        ):
            preflight.validate_recipe_catalog_source_path(catalog)


def test_recipe_catalog_source_path_corrupt_sqlite(tmp_path: Path) -> None:
    catalog = tmp_path / "corrupt.db"
    catalog.write_bytes(b"not an sqlite database file")
    catalog.chmod(0o600)
    with pytest.raises(
        preflight.DeployPreflightError, match="invalid-recipe-catalog-database"
    ):
        preflight.validate_recipe_catalog_source_path(catalog)


def test_recipe_catalog_source_path_missing_metadata_table(tmp_path: Path) -> None:
    catalog = tmp_path / "no_meta.db"
    conn = sqlite3.connect(catalog)
    conn.execute("CREATE TABLE dummy(id INT);")
    conn.commit()
    conn.close()
    catalog.chmod(0o600)
    with pytest.raises(
        preflight.DeployPreflightError, match="invalid-recipe-catalog-schema"
    ):
        preflight.validate_recipe_catalog_source_path(catalog)


def test_recipe_catalog_source_path_unsupported_schema(tmp_path: Path) -> None:
    catalog = tmp_path / "wrong_schema.db"
    build_production_catalog(catalog)
    conn = sqlite3.connect(catalog)
    conn.execute(
        "UPDATE catalog_metadata SET value = '99' WHERE key = 'schema_version';"
    )
    conn.commit()
    conn.close()
    catalog.chmod(0o600)
    with pytest.raises(
        preflight.DeployPreflightError, match="invalid-recipe-catalog-schema"
    ):
        preflight.validate_recipe_catalog_source_path(catalog)


def test_recipe_catalog_source_path_fixtures_present(tmp_path: Path) -> None:
    catalog = tmp_path / "fixtures.db"
    build_production_catalog(catalog)
    conn = sqlite3.connect(catalog)
    conn.execute(
        "INSERT INTO recipe_authors (author_id, display_name, created_at) VALUES ('AUTHOR_TEST_01', 'Test Author', '2026-01-01');"
    )
    conn.commit()
    conn.close()
    catalog.chmod(0o600)
    with pytest.raises(
        preflight.DeployPreflightError, match="recipe-catalog-fixtures-present"
    ):
        preflight.validate_recipe_catalog_source_path(catalog)


def test_recipe_catalog_source_path_empty(tmp_path: Path) -> None:
    catalog = tmp_path / "empty.db"
    build_production_catalog(catalog)
    conn = sqlite3.connect(catalog)
    conn.execute("UPDATE catalog_metadata SET value = '0' WHERE key = 'recipe_count';")
    conn.commit()
    conn.close()
    catalog.chmod(0o600)
    with pytest.raises(preflight.DeployPreflightError, match="empty-recipe-catalog"):
        preflight.validate_recipe_catalog_source_path(catalog)


def test_recipe_catalog_source_path_hash_mismatch(tmp_path: Path) -> None:
    catalog = tmp_path / "tampered.db"
    build_production_catalog(catalog)
    conn = sqlite3.connect(catalog)
    conn.execute(
        "UPDATE catalog_metadata SET value = '0000000000000000000000000000000000000000000000000000000000000000' WHERE key = 'content_hash';"
    )
    conn.commit()
    conn.close()
    catalog.chmod(0o600)
    with pytest.raises(
        preflight.DeployPreflightError, match="recipe-catalog-hash-mismatch"
    ):
        preflight.validate_recipe_catalog_source_path(catalog)


def test_recipe_catalog_real_build_passes_all_validations(tmp_path: Path) -> None:
    catalog = tmp_path / "production_recipe_catalog.db"
    content_hash = build_production_catalog(catalog)
    assert content_hash == EXPECTED_CONTENT_HASH
    catalog.chmod(0o600)

    # Validate passes cleanly
    preflight.validate_recipe_catalog_source_path(
        catalog, expected_hash=EXPECTED_CONTENT_HASH
    )


# ============================================================
# 4. CANARY AUTHORITY & TARGET RIGHTS SCOPE VALIDATION
# ============================================================


def test_canary_authority_recipe_grounded_valid() -> None:
    raw = (
        f"{RECIPE_ENABLED}=true\n"
        f"{RECIPE_ALLOWLIST}=1001,1002\n"
        f"HEALBITE_TARGET_RIGHTS_SCOPE=FR,US\n"
    ).encode("utf-8")

    result = deploy._parse_canary_authority(
        raw,
        authorized_features=(RECIPE_FEATURE,),
    )
    assert result[RECIPE_ENABLED] == "true"
    assert result[RECIPE_ALLOWLIST] == "1001,1002"
    assert result["HEALBITE_TARGET_RIGHTS_SCOPE"] == "FR,US"


def test_canary_authority_recipe_grounded_rights_scopes() -> None:
    for scope in ("FR", "US", "FR,US", "US,FR"):
        raw = (
            f"{RECIPE_ENABLED}=true\n"
            f"{RECIPE_ALLOWLIST}=1001\n"
            f"HEALBITE_TARGET_RIGHTS_SCOPE={scope}\n"
        ).encode("utf-8")
        result = deploy._parse_canary_authority(
            raw,
            authorized_features=(RECIPE_FEATURE,),
        )
        assert result["HEALBITE_TARGET_RIGHTS_SCOPE"] == scope


def test_canary_authority_recipe_missing_rights_scope_fails() -> None:
    raw = (f"{RECIPE_ENABLED}=true\n{RECIPE_ALLOWLIST}=1001\n").encode("utf-8")
    with pytest.raises(
        deploy.DeploymentContractError,
        match="canary-recipe-grounded-missing-target-rights-scope",
    ):
        deploy._parse_canary_authority(
            raw,
            authorized_features=(RECIPE_FEATURE,),
        )


def test_canary_authority_recipe_invalid_rights_scope_fails() -> None:
    for invalid in ("UK", "DE", "FR,UK", "OTHER", "fr", "FR,FR", "FR, US"):
        raw = (
            f"{RECIPE_ENABLED}=true\n"
            f"{RECIPE_ALLOWLIST}=1001\n"
            f"HEALBITE_TARGET_RIGHTS_SCOPE={invalid}\n"
        ).encode("utf-8")
        with pytest.raises(
            deploy.DeploymentContractError, match="canary-target-rights-scope-invalid"
        ):
            deploy._parse_canary_authority(
                raw,
                authorized_features=(RECIPE_FEATURE,),
            )


def test_canary_authority_recipe_public_gate_forbidden_in_stage_a() -> None:
    raw = (
        f"{RECIPE_ENABLED}=true\n"
        f"{RECIPE_ALLOWLIST}=1001\n"
        f"HEALBITE_TARGET_RIGHTS_SCOPE=FR\n"
        f"HEALBITE_RECIPE_GROUNDED_MENU_PUBLIC=true\n"
    ).encode("utf-8")
    with pytest.raises(
        deploy.DeploymentContractError, match="canary-recipe-grounded-public-forbidden"
    ):
        deploy._parse_canary_authority(
            raw,
            authorized_features=(RECIPE_FEATURE,),
        )


def test_canary_authority_recipe_unauthorized_feature_fails() -> None:
    raw = (
        f"UNAUTHORIZED_FEATURE_ENABLED=true\nUNAUTHORIZED_FEATURE_ALLOWLIST=1001\n"
    ).encode("utf-8")
    with pytest.raises(
        deploy.DeploymentContractError, match="canary-feature-unauthorized"
    ):
        deploy._parse_canary_authority(
            raw,
            authorized_features=(RECIPE_FEATURE,),
        )


def test_canary_authority_recipe_empty_allowlist_fails() -> None:
    raw = (
        f"{RECIPE_ENABLED}=true\n{RECIPE_ALLOWLIST}=\nHEALBITE_TARGET_RIGHTS_SCOPE=FR\n"
    ).encode("utf-8")
    with pytest.raises(deploy.DeploymentContractError, match="canary-allowlist-empty"):
        deploy._parse_canary_authority(
            raw,
            authorized_features=(RECIPE_FEATURE,),
        )


def test_non_recipe_canary_does_not_require_rights_scope() -> None:
    raw = (
        "HEALBITE_HOUSEHOLDS_ENABLED=true\nHEALBITE_HOUSEHOLDS_ALLOWLIST=1001\n"
    ).encode("utf-8")
    result = deploy._parse_canary_authority(
        raw,
        authorized_features=("HEALBITE_HOUSEHOLDS",),
    )
    assert result["HEALBITE_HOUSEHOLDS_ENABLED"] == "true"
    assert "HEALBITE_TARGET_RIGHTS_SCOPE" not in result


# ============================================================
# 5. EXECUTE CANARY ACTIVATION PRECONDITIONS
# ============================================================


def test_execute_canary_activation_checks_catalog_on_disk(
    protected_contract,
    tmp_path: Path,
    monkeypatch,
) -> None:
    contract, source = protected_contract
    # Ensure recipe_catalog_source is set to nonexistent path
    nonexistent_catalog = tmp_path / "nonexistent" / "recipe_catalog.db"
    contract = replace(contract, recipe_catalog_source=nonexistent_catalog)

    authority_file = tmp_path / "canary.env"
    authority_file.write_text(
        f"{RECIPE_ENABLED}=true\n"
        f"{RECIPE_ALLOWLIST}=1001\n"
        f"HEALBITE_TARGET_RIGHTS_SCOPE=FR,US\n",
        encoding="utf-8",
    )
    authority_file.chmod(0o600)

    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    monkeypatch.setattr(
        deploy, "_validate_runtime_directory", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        deploy.preflight,
        "validate_deployment_lease_owner",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        deploy,
        "_validate_operation_identity",
        lambda *_args, **_kwargs: (SimpleNamespace(image_id=IMAGE_ID), REVISION),
    )
    monkeypatch.setattr(
        deploy.preflight, "acquire_deployment_lease", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        deploy.preflight, "release_deployment_lease", lambda *_args, **_kwargs: None
    )

    monkeypatch.setattr(
        deploy,
        "_read_canary_authority",
        lambda _contract, _source: {
            RECIPE_ENABLED: "true",
            RECIPE_ALLOWLIST: "1001",
            "HEALBITE_TARGET_RIGHTS_SCOPE": "FR,US",
        },
    )
    with pytest.raises(deploy.DeploymentContractError, match="recipe-catalog-missing"):
        deploy.execute_canary_activation(
            contract,
            source=deploy.CANARY_AUTHORITY_PATH,
            image=IMAGE_ID,
            revision=REVISION,
            confirmation=deploy.CANARY_ACTIVATION_CONFIRMATION,
        )
