#!/usr/bin/env python3
"""Bounded, secret-safe runtime attestation for Hermes deployments.

This module performs no deployment mutation.  The canonical production
orchestrator owns serialization, Compose recreation, and rollback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence


IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
FEATURE_NAME_RE = re.compile(r"^HEALBITE_[A-Z0-9_]+_(?:ENABLED|ALLOWLIST)$")
CANONICAL_FEATURE_GATE_PREFIXES = (
    "HEALBITE_HOUSEHOLDS",
    "HEALBITE_INVENTORY_PHOTO",
    "HEALBITE_INVENTORY_PHOTO_UI",
    "HEALBITE_INVENTORY_TEXT",
    "HEALBITE_INVENTORY_TEXT_UI",
    "HEALBITE_INVENTORY_WEEKLY_GENERATION_UI",
    "HEALBITE_SHOPPING_LIST",
    "HEALBITE_WEEKLY_MENU",
    "HEALBITE_WEEKLY_MENU_INVENTORY",
)
CANONICAL_FEATURE_GATE_NAMES = tuple(
    f"{prefix}_ENABLED" for prefix in CANONICAL_FEATURE_GATE_PREFIXES
)
CANONICAL_ALLOWLIST_NAMES = tuple(
    f"{prefix}_ALLOWLIST" for prefix in CANONICAL_FEATURE_GATE_PREFIXES
)
TRUE_FEATURE_GATE_TOKENS = frozenset({"1", "true", "yes", "on"})
FALSE_FEATURE_GATE_TOKENS = frozenset({"0", "false", "no", "off"})
Run = Callable[..., subprocess.CompletedProcess[str]]
Sleep = Callable[[float], None]


class RuntimeAttestationError(RuntimeError):
    """A safe runtime-attestation classification."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise RuntimeAttestationError(code)


@dataclass(frozen=True)
class RuntimeAttestationPolicy:
    version: int
    startup_observation_seconds: int
    stability_sample_count: int
    stability_interval_seconds: int
    startup_log_tail_lines: int
    startup_log_max_bytes: int
    forbidden_startup_log_classifications: tuple[str, ...]
    telegram_health_command: tuple[str, ...]
    gateway_no_send_command: tuple[str, ...]
    database_structural_contract: tuple[str, ...]
    database_delta_policy: str
    database_schema_change_allowed: bool
    database_unknown_delta_allowed: bool
    feature_gate_names: tuple[str, ...]
    allowlist_names: tuple[str, ...]
    qdrant_non_interference_fields: tuple[str, ...]
    rollback_health_required: bool
    rollback_attempt_count_max: int
    image_only_rollback_db_restore: bool
    evidence_max_bytes: int
    evidence_mode: int


@dataclass(frozen=True)
class MountSnapshot:
    mount_type: str
    source: str
    target: str
    read_only: bool


@dataclass(frozen=True)
class ContainerSnapshot:
    container_id: str
    image_id: str
    revision: str
    created_at: str
    started_at: str
    state: str
    restart_count: int
    mounts: tuple[MountSnapshot, ...]
    feature_gates: tuple[tuple[str, str], ...]
    allowlists: tuple[tuple[str, str, int], ...]
    secret_fingerprints: tuple[tuple[str, str], ...]
    runtime_configuration_fingerprint: str


@dataclass(frozen=True)
class DatabaseSnapshot:
    canonical_path_fingerprint: str
    device: int
    inode: int
    mode: int
    size: int
    user_version: int
    schema_fingerprint: str
    table_fingerprint: str
    index_fingerprint: str
    trigger_fingerprint: str
    integrity_ok: bool
    foreign_key_violations: int
    main_fingerprint: str
    wal_fingerprint: str
    wal_present: bool
    shm_present: bool


@dataclass(frozen=True)
class RuntimeBaseline:
    captured_at: str
    log_cursor: str
    hermes: ContainerSnapshot
    qdrant: ContainerSnapshot
    database: DatabaseSnapshot
    telegram_health: str
    gateway_health: str
    provider_request_count: int


@dataclass(frozen=True)
class PostDeployAttestation:
    observed_at: str
    stability_samples: int
    startup_log_classifications: tuple[tuple[str, int], ...]
    database_structural_result: str
    database_delta_result: str
    feature_gate_delta: str
    allowlist_delta: str
    secret_delta: str
    qdrant_result: str
    telegram_health: str
    gateway_health: str
    provider_request_count: int


def parse_policy(raw: object) -> RuntimeAttestationPolicy:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        _fail("ATTESTATION_POLICY_INVALID")
    expected = {
        "version",
        "startup_observation_seconds",
        "stability_sample_count",
        "stability_interval_seconds",
        "startup_log_tail_lines",
        "startup_log_max_bytes",
        "forbidden_startup_log_classifications",
        "telegram_health_command",
        "gateway_no_send_command",
        "database_structural_contract",
        "database_delta_policy",
        "database_schema_change_allowed",
        "database_unknown_delta_allowed",
        "feature_gate_names",
        "allowlist_names",
        "qdrant_non_interference_fields",
        "rollback_health_required",
        "rollback_attempt_count_max",
        "image_only_rollback_db_restore",
        "evidence_max_bytes",
        "evidence_mode",
    }
    if set(raw) != expected:
        _fail("ATTESTATION_POLICY_FIELDS")

    def integer(name: str, minimum: int, maximum: int) -> int:
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            _fail("ATTESTATION_POLICY_VALUE")
        return value

    def strings(name: str) -> tuple[str, ...]:
        value = raw[name]
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(item, str) and item for item in value)
            or len(set(value)) != len(value)
        ):
            _fail("ATTESTATION_POLICY_VALUE")
        return tuple(value)

    version = integer("version", 1, 1)
    startup_seconds = integer("startup_observation_seconds", 1, 120)
    sample_count = integer("stability_sample_count", 2, 20)
    interval = integer("stability_interval_seconds", 1, 30)
    if (sample_count - 1) * interval > startup_seconds:
        _fail("ATTESTATION_POLICY_VALUE")
    log_tail = integer("startup_log_tail_lines", 1, 2000)
    log_bytes = integer("startup_log_max_bytes", 1024, 1024 * 1024)
    forbidden = strings("forbidden_startup_log_classifications")
    if not all(SAFE_CODE_RE.fullmatch(item) for item in forbidden):
        _fail("ATTESTATION_POLICY_VALUE")
    telegram_command = strings("telegram_health_command")
    gateway_command = strings("gateway_no_send_command")
    for command in (telegram_command, gateway_command):
        if command[:3] != ("docker", "exec", "hermes-bot") or len(command) < 4:
            _fail("ATTESTATION_POLICY_VALUE")
    db_contract = strings("database_structural_contract")
    if db_contract != (
        "user_version",
        "schema",
        "table",
        "index",
        "trigger",
        "integrity",
        "foreign_keys",
    ):
        _fail("ATTESTATION_POLICY_VALUE")
    feature_names = strings("feature_gate_names")
    allowlist_names = strings("allowlist_names")
    if (
        not all(name.endswith("_ENABLED") for name in feature_names)
        or not all(name.endswith("_ALLOWLIST") for name in allowlist_names)
        or set(feature_names) & set(allowlist_names)
        or feature_names != CANONICAL_FEATURE_GATE_NAMES
        or allowlist_names != CANONICAL_ALLOWLIST_NAMES
    ):
        _fail("ATTESTATION_POLICY_VALUE")
    qdrant_fields = strings("qdrant_non_interference_fields")
    if qdrant_fields != (
        "container_id",
        "image_id",
        "created_at",
        "started_at",
        "state",
        "restart_count",
        "mounts",
    ):
        _fail("ATTESTATION_POLICY_VALUE")
    if (
        raw["database_delta_policy"] != "no-delta"
        or raw["database_schema_change_allowed"] is not False
        or raw["database_unknown_delta_allowed"] is not False
        or raw["rollback_health_required"] is not True
        or integer("rollback_attempt_count_max", 1, 1) != 1
        or raw["image_only_rollback_db_restore"] is not False
        or raw["evidence_mode"] != "0600"
    ):
        _fail("ATTESTATION_POLICY_VALUE")
    return RuntimeAttestationPolicy(
        version=version,
        startup_observation_seconds=startup_seconds,
        stability_sample_count=sample_count,
        stability_interval_seconds=interval,
        startup_log_tail_lines=log_tail,
        startup_log_max_bytes=log_bytes,
        forbidden_startup_log_classifications=forbidden,
        telegram_health_command=telegram_command,
        gateway_no_send_command=gateway_command,
        database_structural_contract=db_contract,
        database_delta_policy="no-delta",
        database_schema_change_allowed=False,
        database_unknown_delta_allowed=False,
        feature_gate_names=feature_names,
        allowlist_names=allowlist_names,
        qdrant_non_interference_fields=qdrant_fields,
        rollback_health_required=True,
        rollback_attempt_count_max=1,
        image_only_rollback_db_restore=False,
        evidence_max_bytes=integer("evidence_max_bytes", 1024, 65536),
        evidence_mode=0o600,
    )


def _default_run(
    argv: Sequence[str], *, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _fail("ATTESTATION_COMMAND_FAILED")


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
    except OSError:
        _fail("DATABASE_PATH_UNSAFE")
    return digest.hexdigest()


def _parse_env(values: object) -> dict[str, str]:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        _fail("CONTAINER_INSPECT_INVALID")
    result: dict[str, str] = {}
    for item in values:
        name, separator, value = item.partition("=")
        if not separator or not name or name in result:
            _fail("CONTAINER_ENV_INVALID")
        result[name] = value
    return result


def _feature_gate_state(value: str) -> str:
    token = value.strip().lower()
    if token in TRUE_FEATURE_GATE_TOKENS:
        return "true"
    if token in FALSE_FEATURE_GATE_TOKENS:
        return "false"
    _fail("FEATURE_STATE_INVALID")


def _allowlist_state(value: str) -> tuple[str, int]:
    members = tuple(
        sorted({item.strip() for item in value.replace(";", ",").split(",") if item.strip()})
    )
    return _canonical_hash(members), len(members)


def _container_snapshot(
    service: str,
    *,
    revision_label: str | None,
    feature_gate_names: tuple[str, ...],
    allowlist_names: tuple[str, ...],
    protected_secret_names: tuple[str, ...],
    run: Run,
) -> ContainerSnapshot:
    result = run(("docker", "inspect", service), timeout=30)
    if result.returncode != 0:
        _fail("CONTAINER_INSPECT_FAILED")
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError:
        _fail("CONTAINER_INSPECT_INVALID")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        _fail("CONTAINER_INSPECT_INVALID")
    record = records[0]
    state_record = record.get("State")
    config = record.get("Config")
    mounts_record = record.get("Mounts")
    if not isinstance(state_record, dict) or not isinstance(config, dict) or not isinstance(mounts_record, list):
        _fail("CONTAINER_INSPECT_INVALID")
    labels = config.get("Labels")
    revision = (
        labels.get(revision_label)
        if revision_label is not None and isinstance(labels, dict)
        else ""
    )
    environment = _parse_env(config.get("Env"))
    selected_names = set(feature_gate_names) | set(allowlist_names)
    unknown = {
        name
        for name in environment
        if FEATURE_NAME_RE.fullmatch(name) and name not in selected_names
    }
    if unknown:
        _fail("UNKNOWN_FEATURE_VARIABLE")
    if any(name not in environment for name in selected_names):
        _fail("FEATURE_STATE_MISSING")
    feature_gates = tuple(
        (name, _feature_gate_state(environment[name])) for name in feature_gate_names
    )
    allowlists = tuple(
        (name, *_allowlist_state(environment[name])) for name in allowlist_names
    )
    secret_fingerprints = tuple(
        (name, hashlib.sha256(environment[name].encode("utf-8")).hexdigest())
        for name in protected_secret_names
        if name in environment and environment[name]
    )
    mounts: list[MountSnapshot] = []
    for item in mounts_record:
        if not isinstance(item, dict):
            _fail("CONTAINER_INSPECT_INVALID")
        mount_type = item.get("Type")
        source = item.get("Source")
        target = item.get("Destination")
        read_write = item.get("RW")
        if (
            not isinstance(mount_type, str)
            or not isinstance(source, str)
            or not isinstance(target, str)
            or not isinstance(read_write, bool)
        ):
            _fail("CONTAINER_INSPECT_INVALID")
        mounts.append(
            MountSnapshot(
                mount_type=mount_type,
                source=os.path.normpath(source),
                target=os.path.normpath(target),
                read_only=not read_write,
            )
        )
    mounts_tuple = tuple(
        sorted(mounts, key=lambda item: (item.target, item.source, item.mount_type))
    )
    container_id = record.get("Id")
    image_id = record.get("Image")
    created = record.get("Created")
    started = state_record.get("StartedAt")
    state = state_record.get("Status")
    restart_count = record.get("RestartCount")
    if (
        not isinstance(container_id, str)
        or not container_id
        or not isinstance(image_id, str)
        or not IMAGE_ID_RE.fullmatch(image_id)
        or not isinstance(revision, str)
        or (revision_label is not None and not SHA_RE.fullmatch(revision))
        or not isinstance(created, str)
        or not created
        or not isinstance(started, str)
        or not started
        or not isinstance(state, str)
        or not state
        or isinstance(restart_count, bool)
        or not isinstance(restart_count, int)
        or restart_count < 0
    ):
        _fail("CONTAINER_INSPECT_INVALID")
    configuration_fingerprint = _canonical_hash(
        {
            "mounts": [
                (item.mount_type, item.source, item.target, item.read_only)
                for item in mounts_tuple
            ],
            "feature_gates": feature_gates,
            "allowlists": allowlists,
            "secret_fingerprints": secret_fingerprints,
        }
    )
    return ContainerSnapshot(
        container_id=container_id,
        image_id=image_id,
        revision=revision,
        created_at=created,
        started_at=started,
        state=state,
        restart_count=restart_count,
        mounts=mounts_tuple,
        feature_gates=feature_gates,
        allowlists=allowlists,
        secret_fingerprints=secret_fingerprints,
        runtime_configuration_fingerprint=configuration_fingerprint,
    )


def _qdrant_snapshot(service: str, *, run: Run) -> ContainerSnapshot:
    return _container_snapshot(
        service,
        revision_label=None,
        feature_gate_names=(),
        allowlist_names=(),
        protected_secret_names=(),
        run=run,
    )


def _safe_database_path(path: Path) -> os.stat_result:
    if not path.is_absolute() or path != Path(os.path.normpath(path)):
        _fail("DATABASE_PATH_UNSAFE")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError:
            _fail("DATABASE_PATH_UNSAFE")
        if stat.S_ISLNK(metadata.st_mode):
            _fail("DATABASE_PATH_UNSAFE")
    try:
        metadata = path.lstat()
    except OSError:
        _fail("DATABASE_PATH_UNSAFE")
    if not stat.S_ISREG(metadata.st_mode):
        _fail("DATABASE_PATH_UNSAFE")
    return metadata


def _schema_rows(connection: sqlite3.Connection, kind: str | None = None) -> list[tuple[object, ...]]:
    if kind is None:
        query = (
            "SELECT type,name,tbl_name,coalesce(sql,'') FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name,tbl_name,sql"
        )
        parameters: tuple[str, ...] = ()
    else:
        query = (
            "SELECT type,name,tbl_name,coalesce(sql,'') FROM sqlite_schema "
            "WHERE type=? AND name NOT LIKE 'sqlite_%' ORDER BY name,tbl_name,sql"
        )
        parameters = (kind,)
    return list(connection.execute(query, parameters))


def capture_database(path: Path) -> DatabaseSnapshot:
    metadata = _safe_database_path(path)
    wal_path = Path(f"{path}-wal")
    shm_path = Path(f"{path}-shm")
    wal_present = wal_path.exists()
    shm_present = shm_path.exists()
    wal_fingerprint = _file_hash(wal_path) if wal_present else _canonical_hash("absent")
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            schema = _schema_rows(connection)
            tables = _schema_rows(connection, "table")
            indexes = _schema_rows(connection, "index")
            triggers = _schema_rows(connection, "trigger")
            integrity_rows = list(connection.execute("PRAGMA integrity_check"))
            foreign_key_rows = list(connection.execute("PRAGMA foreign_key_check"))
        finally:
            connection.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        _fail("DATABASE_OPEN_FAILED")
    return DatabaseSnapshot(
        canonical_path_fingerprint=hashlib.sha256(
            os.fsencode(os.path.normpath(path))
        ).hexdigest(),
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        size=metadata.st_size,
        user_version=user_version,
        schema_fingerprint=_canonical_hash(schema),
        table_fingerprint=_canonical_hash(tables),
        index_fingerprint=_canonical_hash(indexes),
        trigger_fingerprint=_canonical_hash(triggers),
        integrity_ok=integrity_rows == [("ok",)],
        foreign_key_violations=len(foreign_key_rows),
        main_fingerprint=_file_hash(path),
        wal_fingerprint=wal_fingerprint,
        wal_present=wal_present,
        shm_present=shm_present,
    )


def _health(
    command: tuple[str, ...],
    *,
    expected_output: tuple[str, ...],
    failure_code: str,
    run: Run,
) -> None:
    result = run(command, timeout=30)
    if result.returncode != 0 or tuple(result.stdout.splitlines()) != expected_output:
        _fail(failure_code)


def _telegram_health(policy: RuntimeAttestationPolicy, *, run: Run) -> None:
    _health(
        policy.telegram_health_command,
        expected_output=("TELEGRAM_HEALTH=PASS",),
        failure_code="TELEGRAM_CONNECTIVITY_FAILED",
        run=run,
    )


def _gateway_health(policy: RuntimeAttestationPolicy, *, run: Run) -> None:
    _health(
        policy.gateway_no_send_command,
        expected_output=("GATEWAY_NO_SEND_SMOKE=PASS", "PROVIDER_REQUESTS=0"),
        failure_code="GATEWAY_SMOKE_FAILED",
        run=run,
    )


def _require_database_healthy(snapshot: DatabaseSnapshot) -> None:
    if not snapshot.integrity_ok:
        _fail("DATABASE_INTEGRITY_FAILED")
    if snapshot.foreign_key_violations:
        _fail("DATABASE_FOREIGN_KEY_VIOLATION")


def _require_expected_runtime(
    snapshot: ContainerSnapshot,
    *,
    expected_image_id: str,
    expected_revision: str,
    expected_mounts: tuple[MountSnapshot, ...],
    expected_feature_gates: tuple[tuple[str, str], ...],
    expected_allowlists: tuple[tuple[str, str, int], ...],
    expected_secret_fingerprints: tuple[tuple[str, str], ...],
) -> None:
    if snapshot.image_id != expected_image_id:
        _fail("HERMES_IMAGE_MISMATCH")
    if snapshot.revision != expected_revision:
        _fail("HERMES_REVISION_MISMATCH")
    if snapshot.state != "running":
        _fail("HERMES_NOT_RUNNING")
    if snapshot.restart_count != 0:
        _fail("HERMES_RESTART_COUNT_CHANGED")
    if snapshot.mounts != expected_mounts:
        _fail("HERMES_MOUNT_SET_CHANGED")
    if snapshot.feature_gates != expected_feature_gates:
        _fail("FEATURE_GATE_DELTA")
    if snapshot.allowlists != expected_allowlists:
        _fail("ALLOWLIST_DELTA")
    if snapshot.secret_fingerprints != expected_secret_fingerprints:
        _fail("SECRET_FINGERPRINT_DELTA")


def capture_pre_mutation_baseline(
    policy: RuntimeAttestationPolicy,
    *,
    hermes_service: str,
    qdrant_service: str,
    database_path: Path,
    database_target: Path,
    revision_label: str,
    protected_secret_names: tuple[str, ...],
    run: Run = _default_run,
) -> RuntimeBaseline:
    log_cursor = datetime.now(timezone.utc).isoformat()
    hermes = _container_snapshot(
        hermes_service,
        revision_label=revision_label,
        feature_gate_names=policy.feature_gate_names,
        allowlist_names=policy.allowlist_names,
        protected_secret_names=protected_secret_names,
        run=run,
    )
    if hermes.state != "running" or hermes.restart_count != 0:
        _fail("PRE_MUTATION_HERMES_UNHEALTHY")
    expected_db_mount = MountSnapshot(
        mount_type="bind",
        source=os.path.normpath(database_path),
        target=os.path.normpath(database_target),
        read_only=False,
    )
    if expected_db_mount not in hermes.mounts:
        _fail("PRE_MUTATION_DATABASE_MOUNT_MISMATCH")
    qdrant = _qdrant_snapshot(qdrant_service, run=run)
    if qdrant.state != "running":
        _fail("PRE_MUTATION_QDRANT_UNHEALTHY")
    database = capture_database(database_path)
    _require_database_healthy(database)
    _telegram_health(policy, run=run)
    _gateway_health(policy, run=run)
    return RuntimeBaseline(
        captured_at=datetime.now(timezone.utc).isoformat(),
        log_cursor=log_cursor,
        hermes=hermes,
        qdrant=qdrant,
        database=database,
        telegram_health="PASS",
        gateway_health="PASS",
        provider_request_count=0,
    )


LOG_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "PYTHON_TRACEBACK": (re.compile(r"\bTraceback \(most recent call last\)", re.I),),
    "UNHANDLED_EXCEPTION": (re.compile(r"\bunhandled (?:exception|error)\b", re.I),),
    "FATAL_STARTUP_FAILURE": (re.compile(r"\b(?:fatal|panic)\b.*\b(?:startup|initiali[sz])", re.I),),
    "DATABASE_FAILURE": (
        re.compile(r"\b(?:database|sqlite)\b.*\b(?:corrupt|integrity|schema|unable to open|failed)\b", re.I),
    ),
    "AUTHENTICATION_FAILURE": (re.compile(r"\b(?:authentication|unauthorized|forbidden)\b", re.I),),
    "SECRET_INVALID": (re.compile(r"\b(?:secret|token|api[_ -]?key)\b.*\b(?:missing|invalid|empty)\b", re.I),),
    "PROVIDER_FALLBACK": (re.compile(r"\bprovider\b.*\bfallback\b", re.I),),
    "GATEWAY_INITIALIZATION_FAILURE": (
        re.compile(r"\bgateway\b.*\b(?:initiali[sz].*failed|failed.*initiali[sz])", re.I),
    ),
    "TELEGRAM_INITIALIZATION_FAILURE": (
        re.compile(r"\btelegram\b.*\b(?:polling|initiali[sz])\b.*\b(?:failed|error)\b", re.I),
    ),
    "CRASH_LOOP": (re.compile(r"\b(?:crash[- ]?loop|restarting repeatedly)\b", re.I),),
}


def classify_startup_logs(
    policy: RuntimeAttestationPolicy,
    *,
    service: str,
    since: str,
    until: str,
    run: Run,
) -> tuple[tuple[str, int], ...]:
    result = run(
        (
            "docker",
            "logs",
            "--since",
            since,
            "--until",
            until,
            "--tail",
            str(policy.startup_log_tail_lines),
            service,
        ),
        timeout=30,
    )
    if result.returncode != 0:
        _fail("STARTUP_LOG_INSPECTION_FAILED")
    payload = (result.stdout + result.stderr).encode("utf-8", errors="replace")
    if len(payload) > policy.startup_log_max_bytes:
        _fail("STARTUP_LOG_BOUND_EXCEEDED")
    text = payload.decode("utf-8", errors="replace")
    counts: list[tuple[str, int]] = []
    for code in policy.forbidden_startup_log_classifications:
        patterns = LOG_PATTERNS.get(code)
        if patterns is None:
            _fail("ATTESTATION_POLICY_VALUE")
        count = sum(len(pattern.findall(text)) for pattern in patterns)
        if count:
            counts.append((code, count))
    if counts:
        _fail(counts[0][0])
    return ()


def _require_database_unchanged(
    before: DatabaseSnapshot, after: DatabaseSnapshot
) -> None:
    _require_database_healthy(after)
    if (
        before.canonical_path_fingerprint != after.canonical_path_fingerprint
        or before.device != after.device
        or before.inode != after.inode
    ):
        _fail("DATABASE_PATH_IDENTITY_CHANGED")
    if before.user_version != after.user_version:
        _fail("DATABASE_USER_VERSION_CHANGED")
    if (
        before.schema_fingerprint != after.schema_fingerprint
        or before.table_fingerprint != after.table_fingerprint
        or before.index_fingerprint != after.index_fingerprint
        or before.trigger_fingerprint != after.trigger_fingerprint
    ):
        _fail("DATABASE_SCHEMA_CHANGED")
    if (
        before.main_fingerprint != after.main_fingerprint
        or before.wal_fingerprint != after.wal_fingerprint
        or before.wal_present != after.wal_present
        or before.shm_present != after.shm_present
    ):
        _fail("DATABASE_DATA_DELTA")


def _require_qdrant_unchanged(
    before: ContainerSnapshot, after: ContainerSnapshot
) -> None:
    if before.container_id != after.container_id:
        _fail("QDRANT_CONTAINER_CHANGED")
    if before.image_id != after.image_id:
        _fail("QDRANT_IMAGE_CHANGED")
    if before.created_at != after.created_at or before.started_at != after.started_at:
        _fail("QDRANT_CREATED_TIME_CHANGED")
    if before.state != after.state:
        _fail("QDRANT_STATE_CHANGED")
    if before.restart_count != after.restart_count:
        _fail("QDRANT_RESTART_COUNT_CHANGED")
    if before.mounts != after.mounts:
        _fail("QDRANT_MOUNT_SET_CHANGED")


def post_deploy_attestation(
    policy: RuntimeAttestationPolicy,
    baseline: RuntimeBaseline,
    *,
    hermes_service: str,
    qdrant_service: str,
    database_path: Path,
    revision_label: str,
    target_image_id: str,
    target_revision: str,
    protected_secret_names: tuple[str, ...],
    run: Run = _default_run,
    sleep: Sleep = time.sleep,
) -> PostDeployAttestation:
    previous_sample: ContainerSnapshot | None = None
    for index in range(policy.stability_sample_count):
        if index:
            sleep(policy.stability_interval_seconds)
        sample = _container_snapshot(
            hermes_service,
            revision_label=revision_label,
            feature_gate_names=policy.feature_gate_names,
            allowlist_names=policy.allowlist_names,
            protected_secret_names=protected_secret_names,
            run=run,
        )
        _require_expected_runtime(
            sample,
            expected_image_id=target_image_id,
            expected_revision=target_revision,
            expected_mounts=baseline.hermes.mounts,
            expected_feature_gates=baseline.hermes.feature_gates,
            expected_allowlists=baseline.hermes.allowlists,
            expected_secret_fingerprints=baseline.hermes.secret_fingerprints,
        )
        if previous_sample is not None and (
            sample.container_id != previous_sample.container_id
            or sample.started_at != previous_sample.started_at
            or sample.restart_count != previous_sample.restart_count
        ):
            _fail("HERMES_LATE_CRASH")
        previous_sample = sample
    if previous_sample is None:
        _fail("HERMES_STABILITY_SAMPLE_MISSING")
    try:
        started_at = datetime.fromisoformat(
            previous_sample.started_at.replace("Z", "+00:00")
        )
    except ValueError:
        _fail("CONTAINER_INSPECT_INVALID")
    log_until = started_at + timedelta(seconds=policy.startup_observation_seconds)
    log_classifications = classify_startup_logs(
        policy,
        service=hermes_service,
        since=previous_sample.started_at,
        until=log_until.isoformat(),
        run=run,
    )
    _telegram_health(policy, run=run)
    _gateway_health(policy, run=run)
    database = capture_database(database_path)
    _require_database_unchanged(baseline.database, database)
    qdrant = _qdrant_snapshot(qdrant_service, run=run)
    _require_qdrant_unchanged(baseline.qdrant, qdrant)
    return PostDeployAttestation(
        observed_at=datetime.now(timezone.utc).isoformat(),
        stability_samples=policy.stability_sample_count,
        startup_log_classifications=log_classifications,
        database_structural_result="PASS",
        database_delta_result="UNCHANGED",
        feature_gate_delta="UNCHANGED",
        allowlist_delta="UNCHANGED",
        secret_delta="UNCHANGED",
        qdrant_result="UNCHANGED",
        telegram_health="PASS",
        gateway_health="PASS",
        provider_request_count=0,
    )


def rollback_log_baseline(baseline: RuntimeBaseline) -> RuntimeBaseline:
    now = datetime.now(timezone.utc).isoformat()
    return replace(baseline, captured_at=now, log_cursor=now)


def _safe_optional_code(value: str | None) -> str | None:
    if value is not None and not SAFE_CODE_RE.fullmatch(value):
        _fail("EVIDENCE_SANITIZATION_FAILED")
    return value


def write_evidence(
    policy: RuntimeAttestationPolicy,
    *,
    runtime_directory: Path,
    target_revision: str,
    target_image_id: str,
    previous_image_id: str,
    operation_status: str,
    post_result: PostDeployAttestation | None,
    original_error: str | None,
    rollback_attempted: bool,
    rollback_result: str,
    rollback_error: str | None,
) -> Path:
    if (
        not SHA_RE.fullmatch(target_revision)
        or not IMAGE_ID_RE.fullmatch(target_image_id)
        or not IMAGE_ID_RE.fullmatch(previous_image_id)
        or operation_status not in {"PASS", "ROLLED_BACK", "FAIL"}
        or rollback_result not in {"NOT_ATTEMPTED", "PASS", "FAIL"}
    ):
        _fail("EVIDENCE_SANITIZATION_FAILED")
    document = {
        "version": policy.version,
        "operation_status": operation_status,
        "target_sha": target_revision,
        "target_image_id": target_image_id,
        "previous_image_id": previous_image_id,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "p0_gates": "PASS",
        "post_check": None
        if post_result is None
        else {
            "stability_samples": post_result.stability_samples,
            "startup_log_classifications": list(post_result.startup_log_classifications),
            "database_structural": post_result.database_structural_result,
            "database_delta": post_result.database_delta_result,
            "feature_gate_delta": post_result.feature_gate_delta,
            "allowlist_delta": post_result.allowlist_delta,
            "secret_delta": post_result.secret_delta,
            "qdrant": post_result.qdrant_result,
            "telegram": post_result.telegram_health,
            "gateway": post_result.gateway_health,
            "provider_request_count": post_result.provider_request_count,
        },
        "original_error": _safe_optional_code(original_error),
        "post_failure": (
            None
            if original_error is None
            else {
                "classification": _safe_optional_code(original_error),
                "count": 1,
                "component": (
                    "hermes-bot"
                    if original_error in policy.forbidden_startup_log_classifications
                    else "deployment-runtime"
                ),
                "relative_observation_seconds": 0,
            }
        ),
        "rollback": {
            "attempted": rollback_attempted,
            "result": rollback_result,
            "error": _safe_optional_code(rollback_error),
            "database_restore": False,
            "qdrant_mutation": False,
        },
        "raw_secret_output_count": 0,
        "raw_identity_output_count": 0,
    }
    payload = (
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if len(payload) > policy.evidence_max_bytes:
        _fail("EVIDENCE_SIZE_EXCEEDED")
    try:
        runtime_metadata = runtime_directory.lstat()
        if (
            stat.S_ISLNK(runtime_metadata.st_mode)
            or not stat.S_ISDIR(runtime_metadata.st_mode)
            or stat.S_IMODE(runtime_metadata.st_mode) != 0o700
        ):
            _fail("EVIDENCE_PATH_UNSAFE")
        name = f"runtime-attestation-{target_revision[:12]}-{time.time_ns()}.json"
        path = runtime_directory / name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, policy.evidence_mode)
        try:
            os.fchmod(fd, policy.evidence_mode)
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:
                    _fail("EVIDENCE_WRITE_FAILED")
                written += count
            os.fsync(fd)
        finally:
            os.close(fd)
        if stat.S_IMODE(path.lstat().st_mode) != policy.evidence_mode:
            _fail("EVIDENCE_MODE_INVALID")
        directory_fd = os.open(runtime_directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except RuntimeAttestationError:
        raise
    except OSError:
        _fail("EVIDENCE_WRITE_FAILED")
    return path
