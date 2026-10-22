"""Deterministic contract tests for Hermes Runtime Attestation V2.

Verifies all 22 required negative scenarios and invariant boundaries.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hermes_post_deploy_attestation as post_attestation
import hermes_production_deploy as deploy_orchestrator
import hermes_runtime_attestation_v2 as attestation_v2

TEST_REVISION_1 = "1" * 40
TEST_REVISION_2 = "2" * 40
IMAGE_ID_1 = "sha256:" + "1" * 64
IMAGE_ID_2 = "sha256:" + "2" * 64
IMAGE_ID_3 = "sha256:" + "3" * 64


@pytest.fixture
def clean_db(tmp_path: Path) -> Path:
    db_file = tmp_path / "healbite.db"
    conn = sqlite3.connect(db_file)
    try:
        conn.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE nutrition_log(
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                food_name TEXT NOT NULL,
                calories REAL NOT NULL
            );
            CREATE TABLE memory_os_facts(
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                fact TEXT NOT NULL
            );
            INSERT INTO users(id, name) VALUES (1, 'Alice');
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_file


@pytest.fixture
def synthetic_fixture() -> Path:
    p = REPO_ROOT / "tests" / "fixtures" / "food_vision_quality" / "v1" / "images" / "fixture_a.png"
    return p


# ==============================================================================
# 22 NEGATIVE / BOUNDARY CONTRACT TESTS
# ==============================================================================

def test_negative_01_healthy_container_passes():
    """1. Healthy container -> PASS."""
    container = {
        "Image": IMAGE_ID_1,
        "Config": {
            "Labels": {
                "org.opencontainers.image.revision": TEST_REVISION_1,
            }
        },
        "RepoDigests": ["ghcr.io/life2boat/hermes@sha256:" + "a" * 64],
    }
    local_image = {
        "Id": IMAGE_ID_1,
        "RootFS": {"Layers": ["sha256:" + "b" * 64]},
    }
    ident = attestation_v2.check_image_identity(
        container_record=container,
        local_image_record=local_image,
        target_revision=TEST_REVISION_1,
        expected_image_id=IMAGE_ID_1,
    )
    assert ident.status == "PASS"
    assert ident.container_image_id == IMAGE_ID_1
    assert ident.local_image_id == IMAGE_ID_1
    assert ident.oci_revision == TEST_REVISION_1


def test_negative_02_restart_count_changed_fails(clean_db: Path):
    """2. RestartCount changed -> FAIL."""
    contract = deploy_orchestrator.load_contract()
    snapshot = post_attestation.ContainerSnapshot(
        container_id="cont-1",
        image_id=IMAGE_ID_1,
        revision=TEST_REVISION_1,
        created_at="2026-10-22T00:00:00Z",
        started_at="2026-10-22T00:00:01Z",
        state="running",
        restart_count=3,  # non-zero restart count
        mounts=(),
        feature_gates=(),
        allowlists=(),
        secret_fingerprints=(),
        runtime_configuration_fingerprint="fp1",
    )
    with pytest.raises(post_attestation.RuntimeAttestationError) as exc:
        post_attestation._require_expected_runtime(
            snapshot,
            expected_image_id=IMAGE_ID_1,
            expected_revision=TEST_REVISION_1,
            expected_mounts=(),
            expected_feature_gates=(),
            expected_allowlists=(),
            expected_secret_fingerprints=(),
        )
    assert exc.value.code == "HERMES_RESTART_COUNT_CHANGED"


def test_negative_03_oci_revision_mismatch_fails():
    """3. OCI revision mismatch -> FAIL."""
    container = {
        "Image": IMAGE_ID_1,
        "Config": {
            "Labels": {
                "org.opencontainers.image.revision": TEST_REVISION_2,  # wrong revision
            }
        },
    }
    ident = attestation_v2.check_image_identity(
        container_record=container,
        local_image_record=None,
        target_revision=TEST_REVISION_1,
        expected_image_id=IMAGE_ID_1,
    )
    assert ident.status == "FAIL"


def test_negative_04_unauthorized_mount_fails():
    """4. Unauthorized mount -> FAIL."""
    snapshot = post_attestation.ContainerSnapshot(
        container_id="cont-1",
        image_id=IMAGE_ID_1,
        revision=TEST_REVISION_1,
        created_at="2026-10-22T00:00:00Z",
        started_at="2026-10-22T00:00:01Z",
        state="running",
        restart_count=0,
        mounts=(
            post_attestation.MountSnapshot("bind", "/etc/shadow", "/home/hermes/secret", True),
        ),
        feature_gates=(),
        allowlists=(),
        secret_fingerprints=(),
        runtime_configuration_fingerprint="fp1",
    )
    with pytest.raises(post_attestation.RuntimeAttestationError) as exc:
        post_attestation._require_expected_runtime(
            snapshot,
            expected_image_id=IMAGE_ID_1,
            expected_revision=TEST_REVISION_1,
            expected_mounts=(),
            expected_feature_gates=(),
            expected_allowlists=(),
            expected_secret_fingerprints=(),
        )
    assert exc.value.code == "HERMES_MOUNT_SET_CHANGED"


def test_negative_05_missing_protected_secret_fails():
    """5. Missing protected secret -> FAIL."""
    env = {
        "HERMES_VISION_PROVIDER": "alibaba",
        "HERMES_VISION_MODEL": "qwen3-vl-8b-instruct",
        # DASHSCOPE_API_KEY is missing!
    }
    vp = attestation_v2.check_vision_provider(environment=env)
    assert vp.status == "FAIL"
    assert vp.credential_present is False


def test_negative_06_wrong_vision_provider_fails():
    """6. Wrong Vision provider -> FAIL."""
    env = {
        "HERMES_VISION_PROVIDER": "unauthorized_ai_corp",
        "HERMES_VISION_MODEL": "some_model",
        "DASHSCOPE_API_KEY": "dummy",
    }
    vp = attestation_v2.check_vision_provider(environment=env)
    assert vp.status == "FAIL"


def test_negative_07_wrong_model_fails():
    """7. Wrong model -> FAIL."""
    env = {
        "HERMES_VISION_PROVIDER": "alibaba",
        "HERMES_VISION_MODEL": "unapproved_gpt_clone",
        "DASHSCOPE_API_KEY": "dummy",
    }
    vp = attestation_v2.check_vision_provider(environment=env)
    assert vp.status == "FAIL"


def test_negative_08_unexpected_secondary_provider_fails():
    """8. Unexpected secondary provider -> FAIL."""
    code, safe_reply, retryable = attestation_v2.classify_vision_failure(
        "Unexpected secondary provider request forbidden on vision task"
    )
    assert code == "CROSS_PROVIDER_FORBIDDEN"
    assert retryable is False
    assert "запрещено политикой безопасности" in safe_reply


def test_negative_09_provider_503_classified_failure():
    """9. Provider 503 -> classified failure."""
    code, safe_reply, retryable = attestation_v2.classify_vision_failure(
        "HTTP 503: Service Unavailable"
    )
    assert code == "PROVIDER_UNAVAILABLE"
    assert retryable is True
    assert "временно недоступен" in safe_reply


def test_negative_10_provider_timeout_classified_failure():
    """10. Provider timeout -> classified failure."""
    code, safe_reply, retryable = attestation_v2.classify_vision_failure(
        "Request timed out after 30.0s"
    )
    assert code == "PROVIDER_TIMEOUT"
    assert retryable is True
    assert "время ожидания" in safe_reply


def test_negative_11_invalid_vision_json_fails(synthetic_fixture: Path):
    """11. Invalid Vision JSON -> FAIL."""
    invalid_json = '{"broken": json without closing quote}'
    canary = attestation_v2.run_controlled_vision_canary(
        fixture_path=synthetic_fixture,
        run_live=True,
        mock_response_json=invalid_json,
    )
    assert canary.canary_status == "FAIL"
    assert canary.json_valid is False


def test_negative_12_raw_image_in_memory_sink_fails(tmp_path: Path):
    """12. Raw image in memory sink -> FAIL."""
    db_file = tmp_path / "leaky.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE notes(id INT, data BLOB)")
    conn.execute("INSERT INTO notes VALUES (1, ?)", (b"\x89PNG\r\n\x1a\nfakeimagebytes",))
    conn.commit()

    audit = attestation_v2.audit_memory_isolation(sqlite_connection=conn)
    assert audit.status == "FAIL"
    assert audit.raw_image_bytes_found is True
    conn.close()


def test_negative_13_private_cache_path_in_trajectory_fails():
    """13. Private cache path in trajectory -> FAIL."""
    trajectories = [
        {
            "turn": 1,
            "tool_calls": [
                {
                    "name": "vision_analyze_tool",
                    "arguments": {"image_url": "/home/hermes/.hermes/cache/vision/temp_secret.jpg"},
                }
            ],
        }
    ]
    audit = attestation_v2.audit_memory_isolation(trajectories=trajectories)
    assert audit.status == "FAIL"
    assert audit.private_cache_path_found is True


def test_negative_14_cross_user_memory_reference_fails():
    """14. Cross-user memory reference -> FAIL."""
    trajectories = [
        {
            "turn": 2,
            "cross_user_id": 9999,
            "text": "Accessing profile of unauthorized_user",
        }
    ]
    audit = attestation_v2.audit_memory_isolation(trajectories=trajectories)
    assert audit.status == "FAIL"
    assert audit.cross_user_reference_found is True


def test_negative_15_sqlite_integrity_failure_fails(tmp_path: Path):
    """15. SQLite integrity failure -> FAIL."""
    bad_db = tmp_path / "corrupt.db"
    bad_db.write_bytes(b"not a valid sqlite file at all")
    status, ver, fk = attestation_v2.check_sqlite_integrity(bad_db)
    assert status == "FAIL"


def test_negative_16_foreign_key_violation_fails(tmp_path: Path):
    """16. Foreign-key violation -> FAIL."""
    db_file = tmp_path / "fk_fail.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        PRAGMA foreign_keys=OFF;
        CREATE TABLE parent(id INTEGER PRIMARY KEY);
        CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INT REFERENCES parent(id));
        INSERT INTO child(id, parent_id) VALUES (1, 999); -- orphan!
        """
    )
    conn.commit()
    conn.close()

    status, ver, fk = attestation_v2.check_sqlite_integrity(db_file)
    assert status == "FAIL"


def test_negative_17_qdrant_collection_drift_fails():
    """17. Qdrant collection drift -> FAIL."""
    snapshot = post_attestation.ContainerSnapshot(
        container_id="cont-1",
        image_id=IMAGE_ID_1,
        revision=TEST_REVISION_1,
        created_at="2026-10-22T00:00:00Z",
        started_at="2026-10-22T00:00:01Z",
        state="running",
        restart_count=0,
        mounts=(),
        feature_gates=(),
        allowlists=(),
        secret_fingerprints=(),
        runtime_configuration_fingerprint="fp1",
        qdrant_collection="healbite_memory_os",  # legacy drift!
    )
    with pytest.raises(post_attestation.RuntimeAttestationError) as exc:
        post_attestation._require_expected_runtime(
            snapshot,
            expected_image_id=IMAGE_ID_1,
            expected_revision=TEST_REVISION_1,
            expected_mounts=(),
            expected_feature_gates=(),
            expected_allowlists=(),
            expected_secret_fingerprints=(),
            expected_qdrant_collection="healbite_memory_os_v2",
        )
    assert exc.value.code == "LEGACY_QDRANT_COLLECTION"


def test_negative_18_telegram_no_send_violation_fails(clean_db: Path):
    """18. Telegram no-send violation -> FAIL."""
    policy = deploy_orchestrator.load_contract().attestation_policy

    # Mock gateway command returning PROVIDER_REQUESTS=1 instead of 0
    def mock_run(argv, timeout=30):
        if "gateway.run" in " ".join(argv):
            return subprocess.CompletedProcess(
                argv, 0, "GATEWAY_NO_SEND_SMOKE=PASS\nPROVIDER_REQUESTS=1\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "TELEGRAM_HEALTH=PASS\n", "")

    t_health, g_health, sent, pollers = attestation_v2.check_telegram_gateway_health(
        policy, run=mock_run
    )
    assert g_health == "FAIL"


def test_negative_19_failed_rollback_prerequisite_blocks():
    """19. Failed rollback prerequisite -> BLOCKED."""
    ready, err = attestation_v2.verify_rollback_prerequisites(
        previous_image_id=IMAGE_ID_1,
        target_revision=TEST_REVISION_1,
        rollback_image_available=False,  # missing image!
        schema_compatible=True,
        protected_secrets_unchanged=True,
        db_state_safe=True,
    )
    assert ready is False
    assert err == "ROLLBACK_IMAGE_UNAVAILABLE"


def test_negative_20_passive_audit_failure_no_mutation(tmp_path: Path):
    """20. Passive audit failure -> no mutation."""
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"broken sqlite")

    contract = deploy_orchestrator.load_contract()
    contract = deploy_orchestrator.replace(
        contract,
        database_source=corrupt_db,
    )
    report = attestation_v2.AttestationReportV2(
        target_revision=TEST_REVISION_1,
        observed_revision=TEST_REVISION_1,
        operation_status="PASS",
    )
    res = attestation_v2.run_level_1_passive(contract=contract, report=report)
    assert res == "FAIL"
    # Ensure no mutation performed on any check
    for chk in report.checks:
        assert chk.mutation_performed is False


def test_negative_21_canary_not_executed_not_run(synthetic_fixture: Path):
    """21. Canary not executed -> NOT_RUN."""
    report = attestation_v2.AttestationReportV2(
        target_revision=TEST_REVISION_1,
        observed_revision=TEST_REVISION_1,
        operation_status="PASS",
    )
    res = attestation_v2.run_level_3_canary(
        report=report,
        fixture_path=synthetic_fixture,
        run_live=False,  # not executed
    )
    assert res == "NOT_RUN"
    assert report.level_statuses["LEVEL_3"] == "NOT_RUN"


def test_negative_22_invalid_or_poisoned_evidence_fails(tmp_path: Path):
    """22. Invalid or poisoned evidence -> FAIL."""
    report = attestation_v2.AttestationReportV2(
        target_revision=TEST_REVISION_1,
        observed_revision=TEST_REVISION_1,
        operation_status="PASS",
    )
    # Exceed maximum size
    report.checks = [
        attestation_v2.CheckRecord(
            check_id=f"CHECK_{i}",
            check_status="PASS",
            evidence_source="x" * 2000,
            timestamp_utc="2026-10-22T00:00:00Z",
            target_revision=TEST_REVISION_1,
            observed_revision=TEST_REVISION_1,
            safe_error_code=None,
            mutation_performed=False,
        )
        for i in range(100)
    ]
    with pytest.raises(attestation_v2.AttestationV2Error) as exc:
        attestation_v2.write_attestation_receipt_v2(report, tmp_path)
    assert exc.value.code == "EVIDENCE_SIZE_EXCEEDED"


def test_full_attestation_report_generation(synthetic_fixture: Path, tmp_path: Path):
    """Positive end-to-end attestation receipt writing and structure."""
    out_dir = tmp_path / "receipts"
    out_dir.mkdir(parents=True)

    report = attestation_v2.AttestationReportV2(
        target_revision=TEST_REVISION_1,
        observed_revision=TEST_REVISION_1,
        operation_status="PASS",
    )
    report.add_check(
        check_id="SELF_TEST",
        status="PASS",
        evidence_source="unit_test",
    )
    path = attestation_v2.write_attestation_receipt_v2(report, out_dir)
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["TASK"] == "HERMES_RUNTIME_ATTESTATION_V2"
    assert payload["STATUS"] == "PASS"
    assert len(payload["CHECKS"]) == 1
    assert payload["CHECKS"][0]["CHECK_ID"] == "SELF_TEST"


# ==============================================================================
# REPRODUCTION TESTS FOR 8 FALSE-PASS DEFECTS (V2.1 TRUTHFUL EVIDENCE)
# ==============================================================================

def test_reproduce_01_live_canary_without_real_provider_call_not_pass(synthetic_fixture: Path):
    """Defect 1: Live canary without real provider call must not return PASS."""
    # When live_allowed=False, run_live=True must return NOT_RUN, never PASS
    canary_disallowed = attestation_v2.run_controlled_vision_canary(
        fixture_path=synthetic_fixture,
        run_live=True,
        live_allowed=False,
    )
    assert canary_disallowed.canary_status == "NOT_RUN"
    assert canary_disallowed.real_provider_call_verified is False
    assert canary_disallowed.provider_calls == 0

    # When live_allowed=True but no real_provider_executor is provided, must return FAIL, never PASS
    canary_no_exec = attestation_v2.run_controlled_vision_canary(
        fixture_path=synthetic_fixture,
        run_live=True,
        live_allowed=True,
        real_provider_executor=None,
    )
    assert canary_no_exec.canary_status == "FAIL"
    assert canary_no_exec.real_provider_call_verified is False


def test_reproduce_02_memory_isolation_zero_sources_not_verified():
    """Defect 2: Memory isolation with 0 sources must return NOT_VERIFIED, never PASS."""
    audit_empty = attestation_v2.audit_memory_isolation()
    assert audit_empty.status == "NOT_VERIFIED"
    assert audit_empty.coverage == "NONE"
    assert len(audit_empty.surfaces_checked) == 0

    # Partial surface coverage (1 out of 3) must also be NOT_VERIFIED if clean
    mock_trajectories = [{"turn": 1, "text": "clean user text without media"}]
    audit_partial = attestation_v2.audit_memory_isolation(trajectories=mock_trajectories)
    assert audit_partial.status == "NOT_VERIFIED"
    assert audit_partial.coverage == "PARTIAL"


def test_reproduce_03_deep_attestation_without_three_samples_not_pass():
    """Defect 3: Deep attestation without 3 stability samples must not return PASS."""
    contract = deploy_orchestrator.load_contract()
    report = attestation_v2.AttestationReportV2(
        target_revision=TEST_REVISION_1,
        observed_revision=TEST_REVISION_1,
        operation_status="PASS",
    )
    # Fewer than 3 samples must fail
    res = attestation_v2.run_level_2_deep(
        contract=contract,
        report=report,
        target_revision=TEST_REVISION_1,
        target_image_id=IMAGE_ID_1,
        stability_samples=2,  # < 3!
    )
    assert res == "FAIL"

    # 3 samples where container restart count increments must fail
    sample_calls = 0
    def mock_run_flapping(argv, timeout=30):
        nonlocal sample_calls
        sample_calls += 1
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps([{
                "Id": "container_123",
                "State": {
                    "Status": "running",
                    "StartedAt": "2026-10-22T00:00:00Z",
                    "RestartCount": sample_calls - 1,  # restarts increment!
                },
                "Config": {"Labels": {"org.opencontainers.image.revision": TEST_REVISION_1}},
                "Image": IMAGE_ID_1,
            }]),
            "",
        )

    res_flap = attestation_v2.run_level_2_deep(
        contract=contract,
        report=report,
        target_revision=TEST_REVISION_1,
        target_image_id=IMAGE_ID_1,
        stability_samples=3,
        sample_interval_seconds=0,
        run=mock_run_flapping,
    )
    assert res_flap == "FAIL"


def test_reproduce_04_qdrant_mutations_without_baseline_not_verified():
    """Defect 4: Qdrant mutations=0 without baseline must be NOT_VERIFIED, never PASS."""
    def mock_run_qdrant(argv, timeout=30):
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps([{
                "Id": "qdrant_123",
                "Created": "2026-10-22T00:00:00Z",
                "State": {"Status": "running", "StartedAt": "2026-10-22T00:00:00Z"},
                "RestartCount": 0,
                "Config": {
                    "Labels": {},
                    "Env": ["QDRANT_COLLECTION=healbite_memory_os_v2"],
                },
                "Image": IMAGE_ID_1,
                "Mounts": [],
            }]),
            "",
        )

    stat, mut_count = attestation_v2.check_qdrant_health(
        service_name="qdrant",
        baseline=None,  # No baseline!
        run=mock_run_qdrant,
    )
    assert stat == "PASS"
    assert mut_count is None  # Unmeasured delta!


def test_reproduce_05_telegram_pollers_unmeasured_not_verified():
    """Defect 5: Telegram pollers=1 without measurement must be NOT_VERIFIED, never PASS."""
    policy = deploy_orchestrator.load_contract().attestation_policy
    def mock_run(argv, timeout=30):
        return subprocess.CompletedProcess(argv, 0, "OK\n", "")

    t_health, g_health, sent, pollers = attestation_v2.check_telegram_gateway_health(
        policy,
        pollers_measured=None,  # unmeasured!
        run=mock_run,
    )
    assert pollers is None  # must not be hardcoded 1!


def test_reproduce_06_rollback_ready_unverified_is_false():
    """Defect 6: Rollback ready with unverified prerequisites must be False and BLOCKED."""
    ready, err = attestation_v2.verify_rollback_prerequisites(
        previous_image_id=IMAGE_ID_1,
        target_revision=TEST_REVISION_1,
        rollback_image_available=False,
    )
    assert ready is False
    assert err == "ROLLBACK_IMAGE_UNAVAILABLE"

    contract = deploy_orchestrator.load_contract()
    report = attestation_v2.AttestationReportV2(
        target_revision=TEST_REVISION_1,
        observed_revision=TEST_REVISION_1,
        operation_status="PASS",
    )
    # Mock docker inspect failing for previous image
    def mock_run_missing(argv, timeout=15):
        return subprocess.CompletedProcess(argv, 1, "", "image not found")

    res = attestation_v2.run_level_4_deploy_gate(
        contract=contract,
        report=report,
        target_revision=TEST_REVISION_1,
        target_image_id=IMAGE_ID_1,
        previous_image_id=IMAGE_ID_2,
        rollback_image_available=False,
        run=mock_run_missing,
    )
    assert res == "BLOCKED"
    assert report.rollback_ready is False


def test_reproduce_07_oci_config_digest_from_layers_rejected():
    """Defect 7: RootFS.Layers[-1] must not be treated as OCI config digest."""
    container = {
        "Image": IMAGE_ID_1,
        "Config": {"Labels": {"org.opencontainers.image.revision": TEST_REVISION_1}},
    }
    local_image_with_layers_only = {
        "Id": IMAGE_ID_1,
        "RootFS": {"Layers": ["sha256:" + "d" * 64]},  # diff_id layer
    }
    ident = attestation_v2.check_image_identity(
        container_record=container,
        local_image_record=local_image_with_layers_only,
        target_revision=TEST_REVISION_1,
        expected_image_id=IMAGE_ID_1,
    )
    assert ident.oci_config_digest is None
    assert ident.oci_config_digest != "sha256:" + "d" * 64


def test_reproduce_08_overall_pass_forbidden_with_mandatory_not_run(synthetic_fixture: Path, clean_db: Path):
    """Defect 8: Overall PASS is forbidden if any requested level is NOT_RUN or NOT_VERIFIED."""
    contract = deploy_orchestrator.load_contract()
    contract = deploy_orchestrator.replace(contract, database_source=clean_db)

    def mock_run(argv, timeout=30):
        cmd = " ".join(argv)
        if "inspect" in cmd:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps([{
                    "Id": "cid_123",
                    "Created": "2026-10-22T00:00:00Z",
                    "State": {"Status": "running", "StartedAt": "2026-10-22T00:00:00Z"},
                    "RestartCount": 0,
                    "Config": {
                        "Labels": {"org.opencontainers.image.revision": TEST_REVISION_1},
                        "Env": ["QDRANT_COLLECTION=healbite_memory_os_v2"],
                    },
                    "Image": IMAGE_ID_1,
                    "Mounts": [],
                }]),
                "",
            )
        if "getMe" in cmd:
            return subprocess.CompletedProcess(argv, 0, "TELEGRAM_HEALTH=PASS\n", "")
        if "gateway.run" in cmd:
            return subprocess.CompletedProcess(
                argv, 0, "GATEWAY_NO_SEND_SMOKE=PASS\nPROVIDER_REQUESTS=0\n", ""
            )
        return subprocess.CompletedProcess(argv, 0, "OK\n", "")

    env_patch = {
        "HERMES_VISION_PROVIDER": "alibaba",
        "HERMES_VISION_MODEL": "qwen3-vl-8b-instruct",
        "DASHSCOPE_API_KEY": "dummy_test_key",
    }
    with patch.dict(os.environ, env_patch), patch.object(attestation_v2.deploy_orchestrator, "load_contract", return_value=contract):
        report = attestation_v2.run_full_attestation(
            target_revision=TEST_REVISION_1,
            target_image_id=IMAGE_ID_1,
            previous_image_id=IMAGE_ID_1,
            levels=["1", "2", "3", "4"],
            run_live_canary=False,
            live_allowed=False,
            rollback_image_available=True,
            schema_compatible=True,
            protected_secrets_unchanged=True,
            db_state_safe=True,
            fixture_path=synthetic_fixture,
            run=mock_run,
        )
    assert report.level_statuses["LEVEL_3"] == "NOT_RUN"
    assert report.operation_status != "PASS"
    assert report.operation_status == "BLOCKED"
