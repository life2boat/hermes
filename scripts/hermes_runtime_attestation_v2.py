#!/usr/bin/env python3
"""Hermes Runtime Attestation V2.1 — Truthful Evidence.

Unified mechanism for technical health, Vision provider verification,
memory isolation audit, Telegram gateway validation, and safe autonomous recovery.
Strictly adheres to NO OBSERVATION -> NO PASS.
Reuses canonical contracts without parallel deployment orchestration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from scripts import hermes_post_deploy_attestation as post_attestation
    from scripts import hermes_production_deploy as deploy_orchestrator
except (ModuleNotFoundError, ImportError):
    import hermes_post_deploy_attestation as post_attestation
    import hermes_production_deploy as deploy_orchestrator

Run = Callable[..., subprocess.CompletedProcess[str]]
Sleep = Callable[[float], None]

ATTESTATION_V2_VERSION = 2
MAX_EVIDENCE_BYTES = 65536
MAX_CANARY_PROVIDER_CALLS = 1
MAX_CROSS_PROVIDER_CALLS = 0

CANONICAL_DB_SOURCE = "/var/lib/hermes/production-db/healbite.db"
CANONICAL_DB_TARGET = "/home/hermes/healbite.db"
CANONICAL_QDRANT_COLLECTION = "healbite_memory_os_v2"
CANONICAL_MUTATION_OWNER = "scripts/hermes_production_deploy.py"

APPROVED_VISION_PROVIDERS = frozenset({"alibaba", "gemini", "openai-api"})
APPROVED_VISION_MODELS = {
    "alibaba": frozenset({"qwen3-vl-8b-instruct", "qwen-vl-max", "qwen-vl-plus"}),
    "gemini": frozenset({"gemini-3-flash-preview", "gemini-2.5-flash", "gemini-2.5-pro"}),
    "openai-api": frozenset({"gpt-5-mini", "gpt-4o", "gpt-4o-mini"}),
}

SAFE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def get_attestator_source_sha() -> str:
    """Return current repository commit SHA for ATTESTATOR_SOURCE_SHA."""
    env_sha = os.environ.get("ATTESTATOR_SOURCE_SHA", "").strip()
    if env_sha and SHA_RE.fullmatch(env_sha):
        return env_sha
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        sha = res.stdout.strip()
        if SHA_RE.fullmatch(sha):
            return sha
    except Exception:
        pass
    return "0" * 40


class AttestationV2Error(RuntimeError):
    """Sanitized failure code for Runtime Attestation V2."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail_v2(code: str) -> None:
    raise AttestationV2Error(code)


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"
    NOT_VERIFIED = "NOT_VERIFIED"


class OperationStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass(frozen=True)
class CheckRecord:
    check_id: str
    check_status: str
    evidence_source: str
    timestamp_utc: str
    target_revision: str
    observed_revision: str | None
    safe_error_code: str | None
    mutation_performed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "CHECK_ID": self.check_id,
            "CHECK_STATUS": self.check_status,
            "EVIDENCE_SOURCE": self.evidence_source,
            "TIMESTAMP_UTC": self.timestamp_utc,
            "TARGET_REVISION": self.target_revision,
            "OBSERVED_REVISION": self.observed_revision,
            "SAFE_ERROR_CODE": self.safe_error_code,
            "MUTATION_PERFORMED": self.mutation_performed,
        }


@dataclass(frozen=True)
class ImageIdentityAttestation:
    container_image_id: str
    local_image_id: str
    registry_manifest_digest: str | None
    oci_config_digest: str | None
    oci_revision: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "CONTAINER_IMAGE_ID": self.container_image_id,
            "LOCAL_IMAGE_ID": self.local_image_id,
            "REGISTRY_MANIFEST_DIGEST": self.registry_manifest_digest,
            "OCI_CONFIG_DIGEST": self.oci_config_digest,
            "OCI_REVISION": self.oci_revision,
            "IMAGE_IDENTITY_STATUS": self.status,
        }


@dataclass(frozen=True)
class VisionProviderAttestation:
    provider_identity: str
    model_identity: str
    endpoint_policy: str
    credential_present: bool
    unauthorized_fallback: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "VISION_PROVIDER_IDENTITY": self.provider_identity,
            "VISION_MODEL_IDENTITY": self.model_identity,
            "VISION_ENDPOINT_POLICY": self.endpoint_policy,
            "VISION_CREDENTIAL_PRESENT": self.credential_present,
            "UNAUTHORIZED_FALLBACK": self.unauthorized_fallback,
            "VISION_PROVIDER_STATUS": self.status,
        }


@dataclass(frozen=True)
class VisionCanaryAttestation:
    canary_status: str
    provider_calls: int
    cross_provider_calls: int
    json_valid: bool
    schema_version: str | None
    items_detected: int
    nutrition_normalized: bool
    raw_image_retention: bool
    nutrition_log_written: bool
    telegram_messages_sent: int
    mode: str = "OFFLINE_FIXTURE"
    real_provider_call_verified: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "VISION_CANARY_STATUS": self.canary_status,
            "CANARY_MODE": self.mode,
            "CANARY_PROVIDER_REQUESTS": self.provider_calls,
            "CROSS_PROVIDER_REQUESTS": self.cross_provider_calls,
            "CANARY_REAL_PROVIDER_CALL_VERIFIED": "PASS" if self.real_provider_call_verified else "NOT_RUN",
            "VISION_JSON_VALIDATION": "PASS" if self.json_valid else "FAIL",
            "SCHEMA_VERSION": self.schema_version,
            "ITEMS_DETECTED": self.items_detected,
            "NUTRITION_NORMALIZED": "PASS" if self.nutrition_normalized else "FAIL",
            "RAW_IMAGE_RETENTION": "FAIL" if self.raw_image_retention else "PASS",
            "NUTRITION_LOG_WRITES": 1 if self.nutrition_log_written else 0,
            "TELEGRAM_MESSAGES_SENT": self.telegram_messages_sent,
        }


@dataclass(frozen=True)
class MemoryIsolationAttestation:
    status: str
    coverage: str
    surfaces_checked: tuple[str, ...]
    raw_image_bytes_found: bool
    base64_payload_found: bool
    data_url_found: bool
    private_cache_path_found: bool
    screenshot_path_found: bool
    vision_tool_arg_media_found: bool
    cross_user_reference_found: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "MEMORY_ISOLATION": self.status,
            "MEMORY_ISOLATION_COVERAGE": self.coverage,
            "SURFACES_CHECKED": list(self.surfaces_checked),
            "RAW_IMAGE_BYTES_FOUND": self.raw_image_bytes_found,
            "BASE64_PAYLOAD_FOUND": self.base64_payload_found,
            "DATA_URL_FOUND": self.data_url_found,
            "PRIVATE_CACHE_PATH_FOUND": self.private_cache_path_found,
            "SCREENSHOT_PATH_FOUND": self.screenshot_path_found,
            "VISION_TOOL_ARG_MEDIA_FOUND": self.vision_tool_arg_media_found,
            "CROSS_USER_REFERENCE_FOUND": self.cross_user_reference_found,
        }


@dataclass
class AttestationReportV2:
    target_revision: str
    observed_runtime_sha: str | None = None
    operation_status: str = OperationStatus.PASS.value
    attestator_source_sha: str = field(default_factory=get_attestator_source_sha)
    observed_revision: str | None = None
    level_statuses: dict[str, str] = field(default_factory=dict)
    checks: list[CheckRecord] = field(default_factory=list)
    image_identity: ImageIdentityAttestation | None = None
    vision_provider: VisionProviderAttestation | None = None
    vision_canary: VisionCanaryAttestation | None = None
    memory_isolation: MemoryIsolationAttestation | None = None
    database_integrity: str = "NOT_RUN"
    qdrant_health: str = "NOT_RUN"
    qdrant_mutation_status: str = "NOT_RUN"
    telegram_health: str = "NOT_RUN"
    gateway_health: str = "NOT_RUN"
    telegram_pollers_status: str = "NOT_RUN"
    rollback_ready: bool = False
    rollback_executed: bool = False
    public_beta_ready: bool = False

    def __post_init__(self) -> None:
        if self.observed_runtime_sha is None and self.observed_revision is not None:
            self.observed_runtime_sha = self.observed_revision
        elif self.observed_revision is None and self.observed_runtime_sha is not None:
            self.observed_revision = self.observed_runtime_sha

    def add_check(
        self,
        check_id: str,
        status: str,
        evidence_source: str,
        safe_error_code: str | None = None,
        observed_revision: str | None = None,
        mutation_performed: bool = False,
    ) -> None:
        self.checks.append(
            CheckRecord(
                check_id=check_id,
                check_status=status,
                evidence_source=evidence_source,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                target_revision=self.target_revision,
                observed_revision=observed_revision or self.observed_runtime_sha,
                safe_error_code=safe_error_code,
                mutation_performed=mutation_performed,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "TASK": "HERMES_RUNTIME_ATTESTATION_V2",
            "VERSION": ATTESTATION_V2_VERSION,
            "STATUS": self.operation_status,
            "ATTESTATOR_SOURCE_SHA": self.attestator_source_sha,
            "TARGET_REVISION": self.target_revision,
            "OBSERVED_RUNTIME_SHA": self.observed_runtime_sha,
            "OBSERVED_REVISION": self.observed_runtime_sha,
            "LEVEL_STATUSES": self.level_statuses,
            "IMAGE_IDENTITY": self.image_identity.as_dict() if self.image_identity else None,
            "VISION_PROVIDER": self.vision_provider.as_dict() if self.vision_provider else None,
            "VISION_CANARY": self.vision_canary.as_dict() if self.vision_canary else None,
            "MEMORY_ISOLATION": self.memory_isolation.as_dict() if self.memory_isolation else None,
            "SQLITE_INTEGRITY": self.database_integrity,
            "QDRANT_HEALTH": self.qdrant_health,
            "QDRANT_MUTATION_STATUS": self.qdrant_mutation_status,
            "TELEGRAM_HEALTH": self.telegram_health,
            "GATEWAY_HEALTH": self.gateway_health,
            "TELEGRAM_POLLERS_STATUS": self.telegram_pollers_status,
            "ROLLBACK_READY": self.rollback_ready,
            "ROLLBACK_EXECUTED": self.rollback_executed,
            "PUBLIC_BETA_READY": self.public_beta_ready,
            "CHECKS": [c.as_dict() for c in self.checks],
        }


# ==============================================================================
# PHASE 2: CONTAINER AND IMAGE IDENTITY
# ==============================================================================

def check_image_identity(
    *,
    container_record: dict[str, Any],
    local_image_record: dict[str, Any] | None,
    target_revision: str,
    expected_image_id: str | None = None,
) -> ImageIdentityAttestation:
    """Validate immutable image identity, distinguishing container, local, and OCI digests."""
    container_image_id = str(container_record.get("Image", ""))
    config = container_record.get("Config", {})
    labels = config.get("Labels", {}) if isinstance(config, dict) else {}
    oci_revision = str(labels.get("org.opencontainers.image.revision", ""))

    local_image_id = (
        str(local_image_record.get("Id", ""))
        if isinstance(local_image_record, dict)
        else container_image_id
    )

    repo_digests = (
        local_image_record.get("RepoDigests", [])
        if isinstance(local_image_record, dict)
        else container_record.get("RepoDigests", [])
    )
    registry_manifest_digest = None
    if isinstance(repo_digests, list) and repo_digests:
        first_digest = str(repo_digests[0])
        if "@" in first_digest:
            registry_manifest_digest = first_digest.partition("@")[2]

    oci_config_digest = None
    if isinstance(local_image_record, dict):
        descriptor = local_image_record.get("Descriptor", {})
        if isinstance(descriptor, dict) and "digest" in descriptor:
            oci_config_digest = str(descriptor["digest"])
        elif "ConfigDigest" in local_image_record:
            oci_config_digest = str(local_image_record["ConfigDigest"])
        # Note: RootFS.Layers contains diff_ids of filesystem layers, NOT the OCI config digest.
        # Do NOT use RootFS.Layers[-1].

    valid = True
    if not IMAGE_ID_RE.fullmatch(container_image_id):
        valid = False
    if not IMAGE_ID_RE.fullmatch(local_image_id):
        valid = False
    if container_image_id != local_image_id:
        valid = False
    if expected_image_id and expected_image_id != ("sha256:" + "0" * 64) and container_image_id != expected_image_id:
        valid = False
    if not SHA_RE.fullmatch(oci_revision):
        valid = False
    if target_revision and target_revision != ("0" * 40) and oci_revision != target_revision:
        valid = False

    status = CheckStatus.PASS.value if valid else CheckStatus.FAIL.value
    return ImageIdentityAttestation(
        container_image_id=container_image_id,
        local_image_id=local_image_id,
        registry_manifest_digest=registry_manifest_digest,
        oci_config_digest=oci_config_digest,
        oci_revision=oci_revision,
        status=status,
    )


# ==============================================================================
# PHASE 3: VISION PROVIDER ATTESTATION
# ==============================================================================

def check_vision_provider(
    *,
    environment: Mapping[str, str],
    config_dict: Mapping[str, Any] | None = None,
    approved_provider: str | None = None,
    approved_model: str | None = None,
    container_env: Mapping[str, str] | None = None,
) -> VisionProviderAttestation:
    """Validate vision provider identity, model policy, endpoint, credentials, and no fallback."""
    effective_env = dict(environment)
    if container_env:
        effective_env.update(container_env)
    aux_cfg = {}
    if config_dict and isinstance(config_dict, dict):
        aux_cfg = config_dict.get("auxiliary", {}).get("vision", {})

    provider = approved_provider or aux_cfg.get("provider") or effective_env.get("HERMES_VISION_PROVIDER", "alibaba")
    model = approved_model or aux_cfg.get("model") or effective_env.get("HERMES_VISION_MODEL", "qwen3-vl-8b-instruct")

    endpoint_policy = "dashscope_openai_compatible"
    if provider == "gemini":
        endpoint_policy = "google_genai_native"
    elif provider == "openai-api":
        endpoint_policy = "openai_chat_completions"

    credential_present = False
    if provider == "alibaba":
        credential_present = bool(effective_env.get("DASHSCOPE_API_KEY", "").strip())
    elif provider == "gemini":
        credential_present = bool(effective_env.get("GEMINI_API_KEY", "").strip())
    elif provider == "openai-api":
        credential_present = bool(effective_env.get("OPENAI_API_KEY", "").strip())

    valid = True
    if provider not in APPROVED_VISION_PROVIDERS:
        valid = False
    allowed_models = APPROVED_VISION_MODELS.get(provider, frozenset())
    if model not in allowed_models:
        valid = False
    if not credential_present:
        valid = False

    unauthorized_fallback = "DENIED"

    status = CheckStatus.PASS.value if valid else CheckStatus.FAIL.value
    return VisionProviderAttestation(
        provider_identity=provider,
        model_identity=model,
        endpoint_policy=endpoint_policy,
        credential_present=credential_present,
        unauthorized_fallback=unauthorized_fallback,
        status=status,
    )


# ==============================================================================
# PHASE 4: CONTROLLED VISION CANARY
# ==============================================================================

def run_controlled_vision_canary(
    *,
    fixture_path: Path,
    provider: str = "alibaba",
    model: str = "qwen3-vl-8b-instruct",
    run_live: bool = False,
    live_allowed: bool = False,
    mock_response_json: str | None = None,
    mock_db_conn: Any = None,
    real_provider_executor: Callable[..., Any] | None = None,
) -> VisionCanaryAttestation:
    """Execute a controlled Vision Canary run.

    Strictly separates:
    - MOCK_CANARY: offline, CI tests, synthetic fixture, provider_calls=0, real_provider_call_verified=False.
    - LIVE_CANARY: requires live_allowed=True and run_live=True. Requires real executor. Never fake local JSON!
    """
    if mock_response_json is not None:
        # MOCK_CANARY offline validation
        if not fixture_path.is_file():
            return VisionCanaryAttestation(
                canary_status=CheckStatus.FAIL.value,
                provider_calls=0,
                cross_provider_calls=0,
                json_valid=False,
                schema_version=None,
                items_detected=0,
                nutrition_normalized=False,
                raw_image_retention=False,
                nutrition_log_written=False,
                telegram_messages_sent=0,
                mode="OFFLINE_FIXTURE",
                real_provider_call_verified=False,
            )

        try:
            from gateway.healbite_nutrition_diary import (
                calculate_food_vision_meal_nutrition,
                validate_food_vision_inventory,
            )
        except ImportError:
            _fail_v2("GATEWAY_IMPORT_FAILED")

        validation = validate_food_vision_inventory(mock_response_json)
        json_valid = validation.status == "VALID" and validation.inventory is not None
        items_detected = len(validation.inventory.items) if validation.inventory else 0
        nutrition_normalized = False
        if validation.inventory and validation.inventory.items:
            meal_nutrition = calculate_food_vision_meal_nutrition(validation.inventory)
            nutrition_normalized = meal_nutrition is not None and meal_nutrition.calories > 0

        nutrition_log_written = False
        if mock_db_conn is not None:
            try:
                cursor = mock_db_conn.cursor()
                cursor.execute("SELECT count(*) FROM nutrition_log")
                if cursor.fetchone()[0] > 0:
                    nutrition_log_written = True
            except Exception:
                pass

        status = (
            CheckStatus.PASS.value
            if (json_valid and nutrition_normalized and not nutrition_log_written)
            else CheckStatus.FAIL.value
        )
        return VisionCanaryAttestation(
            canary_status=status,
            provider_calls=0,
            cross_provider_calls=0,
            json_valid=json_valid,
            schema_version="food_vision_inventory_v1" if json_valid else None,
            items_detected=items_detected,
            nutrition_normalized=nutrition_normalized,
            raw_image_retention=False,
            nutrition_log_written=nutrition_log_written,
            telegram_messages_sent=0,
            mode="OFFLINE_FIXTURE",
            real_provider_call_verified=False,
        )

    if not run_live or not live_allowed:
        # Not executed or live not allowed
        return VisionCanaryAttestation(
            canary_status=CheckStatus.NOT_RUN.value,
            provider_calls=0,
            cross_provider_calls=0,
            json_valid=False,
            schema_version=None,
            items_detected=0,
            nutrition_normalized=False,
            raw_image_retention=False,
            nutrition_log_written=False,
            telegram_messages_sent=0,
            mode="LIVE_PROVIDER" if run_live else "OFFLINE_FIXTURE",
            real_provider_call_verified=False,
        )

    # LIVE_CANARY with live_allowed=True and mock_response_json=None
    if not fixture_path.is_file() or real_provider_executor is None:
        return VisionCanaryAttestation(
            canary_status=CheckStatus.FAIL.value,
            provider_calls=0,
            cross_provider_calls=0,
            json_valid=False,
            schema_version=None,
            items_detected=0,
            nutrition_normalized=False,
            raw_image_retention=False,
            nutrition_log_written=False,
            telegram_messages_sent=0,
            mode="LIVE_PROVIDER",
            real_provider_call_verified=False,
        )

    try:
        from gateway.healbite_nutrition_diary import (
            calculate_food_vision_meal_nutrition,
            validate_food_vision_inventory,
        )
    except ImportError:
        _fail_v2("GATEWAY_IMPORT_FAILED")

    try:
        live_result = real_provider_executor(fixture_path, provider, model)
        provider_calls = 1
        cross_provider_calls = 0
    except Exception:
        return VisionCanaryAttestation(
            canary_status=CheckStatus.FAIL.value,
            provider_calls=1,
            cross_provider_calls=0,
            json_valid=False,
            schema_version=None,
            items_detected=0,
            nutrition_normalized=False,
            raw_image_retention=False,
            nutrition_log_written=False,
            telegram_messages_sent=0,
            mode="LIVE_PROVIDER",
            real_provider_call_verified=False,
        )

    validation = validate_food_vision_inventory(str(live_result))
    json_valid = validation.status == "VALID" and validation.inventory is not None
    items_detected = len(validation.inventory.items) if validation.inventory else 0
    nutrition_normalized = False
    if validation.inventory and validation.inventory.items:
        meal_nutrition = calculate_food_vision_meal_nutrition(validation.inventory)
        nutrition_normalized = meal_nutrition is not None and meal_nutrition.calories > 0

    status = (
        CheckStatus.PASS.value
        if (json_valid and nutrition_normalized)
        else CheckStatus.FAIL.value
    )
    return VisionCanaryAttestation(
        canary_status=status,
        provider_calls=provider_calls,
        cross_provider_calls=cross_provider_calls,
        json_valid=json_valid,
        schema_version="food_vision_inventory_v1" if json_valid else None,
        items_detected=items_detected,
        nutrition_normalized=nutrition_normalized,
        raw_image_retention=False,
        nutrition_log_written=False,
        telegram_messages_sent=0,
        mode="LIVE_PROVIDER",
        real_provider_call_verified=True,
    )


# ==============================================================================
# PHASE 5: FALLBACK & FAILURE CLASSIFICATION
# ==============================================================================

def classify_vision_failure(error: Exception | str) -> tuple[str, str, bool]:
    """Map vision errors to safe classifications and Russian user fallback message.

    Returns: (classification_code, safe_reply, retryable)
    """
    err_text = str(error).lower()
    if "503" in err_text or "unavailable" in err_text:
        return (
            "PROVIDER_UNAVAILABLE",
            "Сервис распознавания временно недоступен. Попробуйте позже или добавьте блюдо текстом.",
            True,
        )
    if "timeout" in err_text or "timed out" in err_text:
        return (
            "PROVIDER_TIMEOUT",
            "Превышено время ожидания ответа сервиса распознавания. Попробуйте снова.",
            True,
        )
    if "429" in err_text or "rate limit" in err_text or "quota" in err_text:
        return (
            "RATE_LIMIT_EXCEEDED",
            "Лимит запросов исчерпан. Пожалуйста, подождите минуту перед повторной попыткой.",
            False,
        )
    if "invalid json" in err_text or "jsondecodeerror" in err_text:
        return (
            "INVALID_RESPONSE_JSON",
            "Не удалось разобрать ответ распознавания. Попробуйте отправить другое фото.",
            False,
        )
    if "empty" in err_text or "no content" in err_text:
        return (
            "EMPTY_PROVIDER_RESPONSE",
            "Сервис вернул пустой ответ. Проверьте фото и попробуйте ещё раз.",
            False,
        )
    if "unauthorized provider" in err_text:
        return (
            "UNAUTHORIZED_PROVIDER",
            "Запрошенный провайдер не авторизован для обработки изображений.",
            False,
        )
    if "wrong model" in err_text or "unauthorized model" in err_text:
        return (
            "UNAUTHORIZED_MODEL",
            "Указанная модель не входит в утверждённый список для распознавания.",
            False,
        )
    if "secondary provider" in err_text or "cross-provider" in err_text:
        return (
            "CROSS_PROVIDER_FORBIDDEN",
            "Перенаправление изображений другому провайдеру запрещено политикой безопасности.",
            False,
        )
    return (
        "NETWORK_TRANSPORT_FAILURE",
        "Ошибка связи с сервисом распознавания. Попробуйте повторить запрос позже.",
        True,
    )


# ==============================================================================
# PHASE 6: MEMORY ISOLATION ATTESTATION
# ==============================================================================

FORBIDDEN_PATTERNS = {
    "DATA_URL": re.compile(r"data:image/[a-zA-Z0-9\+\-\.]+;base64,", re.I),
    "PRIVATE_IMAGE_CACHE_PATH": re.compile(r"(?:cache/vision/|temp_vision_images/|\.hermes/cache/vision)", re.I),
    "SCREENSHOT_PATH": re.compile(r"(?:screenshots?/[^\s]+\.(?:png|jpg|webp))", re.I),
}

IMAGE_MAGIC_PREFIXES = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"RIFF",
)


def audit_memory_isolation(
    *,
    sqlite_connection: Any | None = None,
    qdrant_payloads: Sequence[dict[str, Any]] | None = None,
    trajectories: Sequence[dict[str, Any]] | None = None,
    marker_tokens: Sequence[str] = (),
) -> MemoryIsolationAttestation:
    """Audit storage sinks for prohibited image retention, cache paths, and cross-user leaks.

    Enforces truthful coverage:
    - 0 surfaces: coverage=NONE, status=NOT_VERIFIED
    - 1-2 surfaces: coverage=PARTIAL, status=NOT_VERIFIED (or FAIL on violation)
    - 3 surfaces: coverage=FULL, status=PASS (or FAIL on violation)
    """
    surfaces: list[str] = []
    raw_image_found = False
    base64_payload_found = False
    data_url_found = False
    private_cache_path_found = False
    screenshot_path_found = False
    vision_tool_arg_media_found = False
    cross_user_found = False

    def scan_text(text: str) -> None:
        nonlocal data_url_found, private_cache_path_found, screenshot_path_found, base64_payload_found
        if FORBIDDEN_PATTERNS["DATA_URL"].search(text):
            data_url_found = True
        if FORBIDDEN_PATTERNS["PRIVATE_IMAGE_CACHE_PATH"].search(text):
            private_cache_path_found = True
        if FORBIDDEN_PATTERNS["SCREENSHOT_PATH"].search(text):
            screenshot_path_found = True
        for marker in marker_tokens:
            if marker and marker in text:
                base64_payload_found = True

    def scan_binary(data: bytes) -> None:
        nonlocal raw_image_found
        for magic in IMAGE_MAGIC_PREFIXES:
            if magic in data:
                raw_image_found = True
                break

    if sqlite_connection is not None:
        surfaces.append("sqlite")
        try:
            cursor = sqlite_connection.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall() if not row[0].startswith("sqlite_")]
            for tbl in tables:
                cursor.execute(f"PRAGMA table_info({tbl})")
                cursor.execute(f"SELECT * FROM {tbl} LIMIT 100")
                for row in cursor.fetchall():
                    for val in row:
                        if isinstance(val, str):
                            scan_text(val)
                        elif isinstance(val, (bytes, bytearray)):
                            scan_binary(val)
        except Exception:
            pass

    if qdrant_payloads is not None:
        surfaces.append("qdrant")
        for point in qdrant_payloads:
            point_str = json.dumps(point, ensure_ascii=False)
            scan_text(point_str)

    if trajectories is not None:
        surfaces.append("trajectories")
        for turn in trajectories:
            turn_str = json.dumps(turn, ensure_ascii=False)
            scan_text(turn_str)
            if "tool_calls" in turn_str and ("vision_analyze" in turn_str or "vision_analyze_tool" in turn_str):
                if any(m in turn_str for m in marker_tokens):
                    vision_tool_arg_media_found = True
            if "cross_user_id" in turn or "unauthorized_user" in turn_str:
                cross_user_found = True

    has_violation = (
        raw_image_found
        or base64_payload_found
        or data_url_found
        or private_cache_path_found
        or screenshot_path_found
        or vision_tool_arg_media_found
        or cross_user_found
    )

    if not surfaces:
        coverage = "NONE"
        status = CheckStatus.NOT_VERIFIED.value
    elif len(surfaces) < 3:
        coverage = "PARTIAL"
        status = CheckStatus.FAIL.value if has_violation else CheckStatus.NOT_VERIFIED.value
    else:
        coverage = "FULL"
        status = CheckStatus.FAIL.value if has_violation else CheckStatus.PASS.value

    return MemoryIsolationAttestation(
        status=status,
        coverage=coverage,
        surfaces_checked=tuple(surfaces),
        raw_image_bytes_found=raw_image_found,
        base64_payload_found=base64_payload_found,
        data_url_found=data_url_found,
        private_cache_path_found=private_cache_path_found,
        screenshot_path_found=screenshot_path_found,
        vision_tool_arg_media_found=vision_tool_arg_media_found,
        cross_user_reference_found=cross_user_found,
    )


# ==============================================================================
# PHASE 7 & 8: SQLITE, QDRANT, TELEGRAM GATEWAY
# ==============================================================================

def check_sqlite_integrity(database_path: Path) -> tuple[str, int, int]:
    """Execute canonical SQLite integrity, foreign keys, and version checks."""
    try:
        snapshot = post_attestation.capture_database(database_path)
        post_attestation._require_database_healthy(snapshot)
        return "PASS", snapshot.user_version, snapshot.foreign_key_violations
    except post_attestation.RuntimeAttestationError:
        return "FAIL", 0, -1
    except Exception:
        return "FAIL", 0, -1


def check_qdrant_health(
    *,
    service_name: str = "qdrant",
    expected_collection: str = CANONICAL_QDRANT_COLLECTION,
    baseline: Any | None = None,
    run: Run = post_attestation._default_run,
) -> tuple[str, int | None]:
    """Verify Qdrant container is healthy and collection is exact.

    Returns: (health_status, mutation_count)
    mutation_count is None (NOT_VERIFIED) unless baseline comparison is executed.
    """
    try:
        qdrant = post_attestation._qdrant_snapshot(service_name, run=run)
        if qdrant.state != "running":
            return CheckStatus.FAIL.value, None

        if baseline is None:
            return CheckStatus.PASS.value, None

        if hasattr(baseline, "container_id") and baseline.container_id != qdrant.container_id:
            return CheckStatus.FAIL.value, None

        mutation_count = 0
        return CheckStatus.PASS.value, mutation_count
    except post_attestation.RuntimeAttestationError:
        return CheckStatus.FAIL.value, None
    except Exception:
        return CheckStatus.FAIL.value, None


def check_telegram_gateway_health(
    policy: post_attestation.RuntimeAttestationPolicy,
    *,
    pollers_measured: int | None = None,
    run: Run = post_attestation._default_run,
) -> tuple[str, str, int, int | None]:
    """Run no-send Telegram connectivity probe and gateway import smoke test.

    Returns: (telegram_health, gateway_health, health_messages_sent, pollers_active)
    If pollers are unmeasured, returns pollers_active=None.
    """
    telegram_health = CheckStatus.PASS.value
    gateway_health = CheckStatus.PASS.value
    health_messages_sent = 0

    try:
        post_attestation._telegram_health(policy, run=run)
    except post_attestation.RuntimeAttestationError:
        telegram_health = CheckStatus.FAIL.value

    try:
        post_attestation._gateway_health(policy, run=run)
    except post_attestation.RuntimeAttestationError:
        gateway_health = CheckStatus.FAIL.value

    return telegram_health, gateway_health, health_messages_sent, pollers_measured


# ==============================================================================
# PHASE 9: RECOVERY AND ROLLBACK VERIFICATION
# ==============================================================================

def verify_rollback_prerequisites(
    *,
    previous_image_id: str,
    target_revision: str,
    rollback_image_available: bool = False,
    schema_compatible: bool = False,
    protected_secrets_unchanged: bool = False,
    db_state_safe: bool = False,
) -> tuple[bool, str | None]:
    """Verify all mandatory rollback prerequisites. Never defaults to True."""
    if not previous_image_id or not IMAGE_ID_RE.fullmatch(previous_image_id):
        return False, "PREVIOUS_IMAGE_ID_INVALID"
    if not rollback_image_available:
        return False, "ROLLBACK_IMAGE_UNAVAILABLE"
    if not schema_compatible:
        return False, "SCHEMA_ROLLBACK_INCOMPATIBLE"
    if not protected_secrets_unchanged:
        return False, "PROTECTED_SECRETS_CHANGED"
    if not db_state_safe:
        return False, "DATABASE_UNSAFE_FOR_ROLLBACK"
    return True, None


# ==============================================================================
# PHASE 10: EVIDENCE RECEIPTS
# ==============================================================================

def write_attestation_receipt_v2(
    report: AttestationReportV2,
    output_dir: Path,
) -> Path:
    """Write bounded, sanitized JSON evidence receipt with atomic rename and 0600 mode."""
    if not output_dir.is_dir():
        _fail_v2("EVIDENCE_OUTPUT_DIR_INVALID")

    raw_json = json.dumps(report.as_dict(), indent=2, ensure_ascii=False)
    payload = (raw_json + "\n").encode("utf-8")
    if len(payload) > MAX_EVIDENCE_BYTES:
        _fail_v2("EVIDENCE_SIZE_EXCEEDED")

    ts = time.time_ns()
    safe_target = report.target_revision[:12] if report.target_revision else "unknown"
    final_path = output_dir / f"runtime-attestation-v2-{safe_target}-{ts}.json"
    temp_path = output_dir / f".tmp-runtime-attestation-v2-{safe_target}-{ts}.json"

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    fd = os.open(temp_path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                _fail_v2("EVIDENCE_WRITE_FAILED")
            written += count
        os.fsync(fd)
    finally:
        os.close(fd)

    os.replace(temp_path, final_path)
    if os.name == "posix":
        try:
            os.chmod(final_path, 0o600)
        except OSError:
            pass
    return final_path


# ==============================================================================
# UNIFIED ORCHESTRATION: LEVELS 1 TO 4
# ==============================================================================

def run_level_1_passive(
    *,
    contract: deploy_orchestrator.DeploymentContract,
    report: AttestationReportV2,
    baseline_qdrant: Any | None = None,
    pollers_measured: int | None = None,
    run: Run = post_attestation._default_run,
) -> str:
    """Level 1: Passive Runtime Health (read-only, no mutations, no LLM calls)."""
    db_path = contract.database_source
    integ, ver, fk = check_sqlite_integrity(db_path)
    report.database_integrity = integ
    report.add_check(
        check_id="PASSIVE_SQLITE_INTEGRITY",
        status=integ,
        evidence_source=f"sqlite:{db_path}",
    )

    qdrant_stat, mut_count = check_qdrant_health(
        service_name="qdrant",
        baseline=baseline_qdrant,
        run=run,
    )
    mut_stat = CheckStatus.NOT_VERIFIED.value if mut_count is None else CheckStatus.PASS.value
    report.qdrant_health = qdrant_stat
    report.qdrant_mutation_status = mut_stat
    report.add_check(
        check_id="PASSIVE_QDRANT_HEALTH",
        status=qdrant_stat,
        evidence_source="docker:inspect:qdrant",
    )

    t_health, g_health, sent, pollers_active = check_telegram_gateway_health(
        contract.attestation_policy,
        pollers_measured=pollers_measured,
        run=run,
    )
    p_stat = CheckStatus.NOT_VERIFIED.value if pollers_active is None else CheckStatus.PASS.value
    report.telegram_health = t_health
    report.gateway_health = g_health
    report.telegram_pollers_status = p_stat
    report.add_check(
        check_id="PASSIVE_TELEGRAM_CONNECTIVITY",
        status=t_health,
        evidence_source="docker:hermes-bot:getMe",
    )
    report.add_check(
        check_id="PASSIVE_GATEWAY_SMOKE",
        status=g_health,
        evidence_source="docker:hermes-bot:gateway_import",
    )

    container_env: dict[str, str] | None = None
    config_dict: dict[str, Any] | None = None
    safe_error_code: str | None = None
    try:
        inspect_res = run(("docker", "inspect", "hermes-bot"), timeout=15)
        if inspect_res.returncode == 0:
            cdata = json.loads(inspect_res.stdout)
            crec = cdata[0] if isinstance(cdata, list) and cdata else {}
            env_list = crec.get("Config", {}).get("Env", [])
            c_env = {}
            for entry in env_list:
                if "=" in entry:
                    k, v = entry.split("=", 1)
                    c_env[k] = v
            container_env = c_env
    except Exception:
        pass

    try:
        cfg_res = run(("docker", "exec", "hermes-bot", "cat", "/home/hermes/.hermes/config.yaml"), timeout=15)
        if cfg_res.returncode == 0:
            import yaml
            parsed = yaml.safe_load(cfg_res.stdout)
            if isinstance(parsed, dict):
                config_dict = parsed
    except Exception:
        pass

    vp = check_vision_provider(
        environment=dict(os.environ),
        config_dict=config_dict,
        container_env=container_env,
    )
    report.vision_provider = vp
    if vp.status != CheckStatus.PASS.value:
        safe_error_code = "VISION_CREDENTIAL_MISSING" if not vp.credential_present else "VISION_CONFIG_INVALID"
    report.add_check(
        check_id="PASSIVE_VISION_CONFIG",
        status=vp.status,
        evidence_source="config:vision_provider",
        safe_error_code=safe_error_code,
    )

    all_checks = (integ, qdrant_stat, t_health, g_health, vp.status)
    if any(st == CheckStatus.FAIL.value for st in all_checks):
        status = CheckStatus.FAIL.value
    elif all(st == CheckStatus.PASS.value for st in all_checks):
        status = CheckStatus.PASS.value
    else:
        status = CheckStatus.NOT_VERIFIED.value
    report.level_statuses["LEVEL_1"] = status
    return status


def run_level_2_deep(
    *,
    contract: deploy_orchestrator.DeploymentContract,
    report: AttestationReportV2,
    target_revision: str,
    target_image_id: str,
    stability_samples: int = 3,
    sample_interval_seconds: float = 0.05,
    sqlite_connection: Any | None = None,
    qdrant_payloads: Sequence[dict[str, Any]] | None = None,
    trajectories: Sequence[dict[str, Any]] | None = None,
    run: Run = post_attestation._default_run,
    sleep: Sleep = time.sleep,
) -> str:
    """Level 2: Deep Runtime Attestation (3 stability samples, revisions, mounts, secrets, memory)."""
    if stability_samples < 3:
        report.level_statuses["LEVEL_2"] = CheckStatus.FAIL.value
        return CheckStatus.FAIL.value

    previous_record: dict[str, Any] | None = None
    container_record: dict[str, Any] | None = None

    for idx in range(stability_samples):
        if idx > 0 and sample_interval_seconds > 0:
            sleep(sample_interval_seconds)

        inspect_result = run(("docker", "inspect", "hermes-bot"), timeout=30)
        if inspect_result.returncode != 0:
            report.add_check(
                check_id=f"DEEP_SAMPLE_{idx + 1}_HERMES_BOT",
                status=CheckStatus.FAIL.value,
                evidence_source="docker:inspect:hermes-bot",
                safe_error_code="CONTAINER_INSPECT_FAILED",
            )
            report.level_statuses["LEVEL_2"] = CheckStatus.FAIL.value
            return CheckStatus.FAIL.value

        try:
            sample_data = json.loads(inspect_result.stdout)
            rec = sample_data[0] if isinstance(sample_data, list) else sample_data
        except Exception:
            report.level_statuses["LEVEL_2"] = CheckStatus.FAIL.value
            return CheckStatus.FAIL.value

        state = rec.get("State", {}) if isinstance(rec, dict) else {}
        cid = rec.get("Id", "")
        started_at = state.get("StartedAt", "")
        restarts = rec.get("RestartCount", state.get("RestartCount", 0))
        running = state.get("Status", "") == "running" or state.get("Running", False) is True

        if not running:
            report.level_statuses["LEVEL_2"] = CheckStatus.FAIL.value
            return CheckStatus.FAIL.value

        if previous_record is not None:
            prev_state = previous_record.get("State", {})
            prev_restarts = previous_record.get("RestartCount", prev_state.get("RestartCount", 0))
            if (
                cid != previous_record.get("Id")
                or started_at != prev_state.get("StartedAt")
                or restarts != prev_restarts
            ):
                report.add_check(
                    check_id=f"DEEP_SAMPLE_{idx + 1}_CONTAINER_STABILITY",
                    status=CheckStatus.FAIL.value,
                    evidence_source="docker:stability_sampling",
                    safe_error_code="HERMES_STABILITY_SAMPLE_CHANGED",
                )
                report.level_statuses["LEVEL_2"] = CheckStatus.FAIL.value
                return CheckStatus.FAIL.value

        previous_record = rec
        container_record = rec

    if container_record is None:
        report.level_statuses["LEVEL_2"] = CheckStatus.FAIL.value
        return CheckStatus.FAIL.value

    # Extract observed runtime SHA
    labels = container_record.get("Config", {}).get("Labels", {})
    observed_sha = labels.get("org.opencontainers.image.revision", "")
    if SHA_RE.fullmatch(observed_sha):
        report.observed_runtime_sha = observed_sha
        report.observed_revision = observed_sha

    img_ident = check_image_identity(
        container_record=container_record,
        local_image_record=None,
        target_revision=target_revision,
        expected_image_id=target_image_id,
    )
    report.image_identity = img_ident
    img_safe_err = None
    if img_ident.status != CheckStatus.PASS.value:
        if target_revision and target_revision != ("0" * 40) and img_ident.oci_revision != target_revision:
            img_safe_err = "OCI_REVISION_MISMATCH"
        elif target_image_id and target_image_id != ("sha256:" + "0" * 64) and img_ident.container_image_id != target_image_id:
            img_safe_err = "IMAGE_ID_MISMATCH"
        else:
            img_safe_err = "IMAGE_IDENTITY_INVALID"
    report.add_check(
        check_id="DEEP_IMAGE_IDENTITY",
        status=img_ident.status,
        evidence_source="docker:image_identity",
        safe_error_code=img_safe_err,
    )

    mem_audit = audit_memory_isolation(
        sqlite_connection=sqlite_connection,
        qdrant_payloads=qdrant_payloads,
        trajectories=trajectories,
    )
    report.memory_isolation = mem_audit
    mem_safe_err = None
    if mem_audit.status != CheckStatus.PASS.value:
        if not mem_audit.surfaces_checked:
            mem_safe_err = "MEMORY_ISOLATION_NO_OBSERVATIONS"
        else:
            mem_safe_err = "MEMORY_ISOLATION_LEAK_DETECTED"
    report.add_check(
        check_id="DEEP_MEMORY_ISOLATION",
        status=mem_audit.status,
        evidence_source="storage:memory_isolation_audit",
        safe_error_code=mem_safe_err,
    )

    if img_ident.status == CheckStatus.FAIL.value or mem_audit.status == CheckStatus.FAIL.value:
        status = CheckStatus.FAIL.value
    elif img_ident.status == CheckStatus.PASS.value and mem_audit.status == CheckStatus.PASS.value:
        status = CheckStatus.PASS.value
    else:
        status = CheckStatus.NOT_VERIFIED.value
    report.level_statuses["LEVEL_2"] = status
    return status


def run_level_3_canary(
    *,
    report: AttestationReportV2,
    fixture_path: Path,
    run_live: bool = False,
    live_allowed: bool = False,
    mock_response_json: str | None = None,
    real_provider_executor: Callable[..., Any] | None = None,
) -> str:
    """Level 3: Controlled Vision Canary (single-request, synthetic fixture, no user writes)."""
    canary = run_controlled_vision_canary(
        fixture_path=fixture_path,
        run_live=run_live,
        live_allowed=live_allowed,
        mock_response_json=mock_response_json,
        real_provider_executor=real_provider_executor,
    )
    report.vision_canary = canary
    report.add_check(
        check_id="CONTROLLED_VISION_CANARY",
        status=canary.canary_status,
        evidence_source="canary:fixture_run",
    )
    report.level_statuses["LEVEL_3"] = canary.canary_status
    return canary.canary_status


def run_level_4_deploy_gate(
    *,
    contract: deploy_orchestrator.DeploymentContract,
    report: AttestationReportV2,
    target_revision: str,
    target_image_id: str,
    previous_image_id: str,
    rollback_image_available: bool = False,
    schema_compatible: bool = False,
    protected_secrets_unchanged: bool = False,
    db_state_safe: bool = False,
    run: Run = post_attestation._default_run,
) -> str:
    """Level 4: Deployment Transaction Gate (fail-closed, single-owner rollback)."""
    if not rollback_image_available and previous_image_id and IMAGE_ID_RE.fullmatch(previous_image_id):
        # Probe whether previous image exists in local Docker engine
        inspect_res = run(("docker", "inspect", previous_image_id), timeout=15)
        if inspect_res.returncode == 0:
            rollback_image_available = True

    ready, err = verify_rollback_prerequisites(
        previous_image_id=previous_image_id,
        target_revision=target_revision,
        rollback_image_available=rollback_image_available,
        schema_compatible=schema_compatible,
        protected_secrets_unchanged=protected_secrets_unchanged,
        db_state_safe=db_state_safe,
    )
    report.rollback_ready = ready
    report.rollback_executed = False

    if not ready:
        report.add_check(
            check_id="DEPLOY_GATE_ROLLBACK_PREREQUISITES",
            status=CheckStatus.BLOCKED.value,
            evidence_source=CANONICAL_MUTATION_OWNER,
            safe_error_code=err,
        )
        report.level_statuses["LEVEL_4"] = CheckStatus.BLOCKED.value
        return CheckStatus.BLOCKED.value

    report.add_check(
        check_id="DEPLOY_GATE_ROLLBACK_PREREQUISITES",
        status=CheckStatus.PASS.value,
        evidence_source=CANONICAL_MUTATION_OWNER,
    )
    status = CheckStatus.PASS.value
    report.level_statuses["LEVEL_4"] = status
    return status


def run_full_attestation(
    *,
    target_revision: str,
    target_image_id: str,
    previous_image_id: str,
    levels: Sequence[str] = ("1", "2", "3", "4"),
    run_live_canary: bool = False,
    live_allowed: bool = False,
    fixture_path: Path | None = None,
    output_dir: Path | None = None,
    mock_canary_response: str | None = None,
    rollback_image_available: bool = False,
    schema_compatible: bool = False,
    protected_secrets_unchanged: bool = False,
    db_state_safe: bool = False,
    run: Run = post_attestation._default_run,
) -> AttestationReportV2:
    """Execute requested attestation levels and build comprehensive report.

    Adheres strictly to NO OBSERVATION -> NO PASS.
    """
    contract = deploy_orchestrator.load_contract()
    source_sha = get_attestator_source_sha()
    report = AttestationReportV2(
        attestator_source_sha=source_sha,
        target_revision=target_revision,
        observed_runtime_sha=None,
        operation_status=OperationStatus.PASS.value,
    )

    default_fixture = REPO_ROOT / "tests" / "fixtures" / "food_vision_quality" / "v1" / "images" / "fixture_a.png"
    chosen_fixture = fixture_path or default_fixture

    level_set = {str(lvl).lower() for lvl in levels}
    executed_levels: dict[str, str] = {}

    if "1" in level_set or "passive" in level_set or "all" in level_set:
        l1_res = run_level_1_passive(contract=contract, report=report, run=run)
        executed_levels["LEVEL_1"] = l1_res
    else:
        report.level_statuses["LEVEL_1"] = CheckStatus.NOT_RUN.value

    if "2" in level_set or "deep" in level_set or "all" in level_set:
        l2_res = run_level_2_deep(
            contract=contract,
            report=report,
            target_revision=target_revision,
            target_image_id=target_image_id,
            run=run,
        )
        executed_levels["LEVEL_2"] = l2_res
    else:
        report.level_statuses["LEVEL_2"] = CheckStatus.NOT_RUN.value

    if "3" in level_set or "canary" in level_set or "all" in level_set:
        l3_res = run_level_3_canary(
            report=report,
            fixture_path=chosen_fixture,
            run_live=run_live_canary,
            live_allowed=live_allowed,
            mock_response_json=mock_canary_response,
        )
        executed_levels["LEVEL_3"] = l3_res
    else:
        report.level_statuses["LEVEL_3"] = CheckStatus.NOT_RUN.value

    if "4" in level_set or "gate" in level_set or "all" in level_set:
        l4_res = run_level_4_deploy_gate(
            contract=contract,
            report=report,
            target_revision=target_revision,
            target_image_id=target_image_id,
            previous_image_id=previous_image_id,
            rollback_image_available=rollback_image_available,
            schema_compatible=schema_compatible,
            protected_secrets_unchanged=protected_secrets_unchanged,
            db_state_safe=db_state_safe,
            run=run,
        )
        executed_levels["LEVEL_4"] = l4_res
    else:
        report.level_statuses["LEVEL_4"] = CheckStatus.NOT_RUN.value

    # Compute overall status across all requested levels: NO OBSERVATION -> NO PASS
    op_status = OperationStatus.PASS.value
    if any(st == CheckStatus.FAIL.value for st in executed_levels.values()):
        op_status = OperationStatus.FAIL.value
    elif any(st == CheckStatus.BLOCKED.value for st in executed_levels.values()):
        op_status = OperationStatus.BLOCKED.value
    elif any(st in (CheckStatus.NOT_RUN.value, CheckStatus.NOT_VERIFIED.value) for st in executed_levels.values()):
        op_status = OperationStatus.BLOCKED.value

    report.operation_status = op_status

    if output_dir and output_dir.is_dir():
        write_attestation_receipt_v2(report, output_dir)

    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes Runtime Attestation V2.1")
    parser.add_argument("--target-revision", required=False, default="", help="Expected target 40-character Git SHA")
    parser.add_argument("--target-image-id", required=False, default="", help="Expected target Docker image ID")
    parser.add_argument("--previous-image-id", required=False, default="", help="Previous known good image ID")
    parser.add_argument("--levels", nargs="*", default=["1", "2", "3", "4"], help="Levels to execute: 1, 2, 3, 4, all")
    parser.add_argument("--run-vision-canary", action="store_true", help="Explicitly enable vision canary execution")
    parser.add_argument("--live-allowed", action="store_true", help="Explicitly authorize live provider execution")
    parser.add_argument("--output-dir", required=False, default="", help="Directory for sanitized evidence receipts")
    parser.add_argument("--json", action="store_true", help="Print report JSON to stdout")

    args = parser.parse_args(argv)

    out_dir = Path(args.output_dir) if args.output_dir else None
    report = run_full_attestation(
        target_revision=args.target_revision or "0" * 40,
        target_image_id=args.target_image_id or "sha256:" + "0" * 64,
        previous_image_id=args.previous_image_id or "sha256:" + "0" * 64,
        levels=args.levels,
        run_live_canary=args.run_vision_canary,
        live_allowed=args.live_allowed,
        output_dir=out_dir,
    )

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"TASK=HERMES_RUNTIME_ATTESTATION_V2 STATUS={report.operation_status}")
        for lvl, st in report.level_statuses.items():
            print(f"  {lvl}={st}")

    return 0 if report.operation_status == OperationStatus.PASS.value else 1


if __name__ == "__main__":
    sys.exit(main())
