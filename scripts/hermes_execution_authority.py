"""Fail-closed execution authority and exact operations-root validation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


MAX_ARTIFACT_BYTES = 1024 * 1024
SHA_RE = re.compile(r"[0-9a-f]{64}")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
EXECUTION_AUTHORITY_VERSION = 1
INVOCATION_DESCRIPTOR_VERSION = 2

EXECUTION_AUTHORITY_FIELDS = frozenset({
    "EXECUTION_AUTHORITY_VERSION",
    "CREATED_AT",
    "EXPIRES_AT",
    "PLAN_PATH",
    "PLAN_SHA256",
    "OPERATIONS_ROOT_APPROVAL_PATH",
    "OPERATIONS_ROOT_APPROVAL_SHA256",
    "CLEAN_START_POLICY_PATH",
    "CLEAN_START_POLICY_SHA256",
    "APPROVAL_ENVELOPE_PATH",
    "APPROVAL_ENVELOPE_SHA256",
    "INVOCATION_DESCRIPTOR_PATH",
    "INVOCATION_DESCRIPTOR_SHA256",
    "PERSISTENT_DB_OVERRIDE_PATH",
    "PERSISTENT_DB_OVERRIDE_SHA256",
    "P5B_EVIDENCE_PATH",
    "P5B_EVIDENCE_SHA256",
    "P6A_F1_EVIDENCE_PATH",
    "P6A_F1_EVIDENCE_SHA256",
    "SOURCE_SHA",
    "SOURCE_TREE_SHA",
    "TARGET_IMAGE_ID",
    "CURRENT_RUNTIME_IMAGE_ID",
    "CANONICAL_PRODUCTION_DB_PATH",
    "SOURCE_DB_SHA256",
    "SOURCE_DB_SIZE",
    "SOURCE_DB_USER_VERSION",
    "SOURCE_DB_SCHEMA_FINGERPRINT",
    "SOURCE_DB_PARENT_IDENTITY",
    "OPERATIONS_ROOT_PATH",
    "OPERATIONS_ROOT_HEAD_SHA",
    "OPERATIONS_ROOT_TREE_SHA",
    "EXECUTION_AUTHORIZED",
    "DEPLOY_AUTHORIZED",
    "CONTAINS_SECRETS",
})

APPROVAL_ENVELOPE_FIELDS = frozenset({
    "ENVELOPE_VERSION",
    "CREATED_AT",
    "PUBLIC_OPERATIONS_ROOT_APPROVAL_PATH",
    "PUBLIC_OPERATIONS_ROOT_APPROVAL_SHA256",
    "OPERATIONS_ROOT_PATH",
    "OPERATIONS_ROOT_HEAD_SHA",
    "OPERATIONS_ROOT_TREE_SHA",
    "OPERATIONS_ROOT_MODE",
    "OPERATIONS_ROOT_UID",
    "OPERATIONS_ROOT_GID",
    "OPERATIONS_ROOT_CLEAN",
    "OBJECT_ALTERNATES_ABSENT",
    "P5B_EVIDENCE_SHA256",
    "P6A_F1_EVIDENCE_SHA256",
    "EXACT_MAIN_IMAGE_ID",
    "CANONICAL_DB_PATH",
    "CANONICAL_DB_DEVICE",
    "CANONICAL_DB_INODE",
    "CANONICAL_DB_SIZE",
    "CANONICAL_DB_SHA256",
    "PERSISTENT_DB_OVERRIDE_SHA256",
    "INVOCATION_DESCRIPTOR_SHA256",
    "CLEAN_START_POLICY_SHA256",
    "PLAN_ONLY_AUTHORIZED",
    "EXECUTION_AUTHORIZED",
    "DEPLOY_AUTHORIZED",
    "CONTAINS_SECRETS",
})

INVOCATION_DESCRIPTOR_FIELDS = frozenset({
    "DESCRIPTOR_VERSION",
    "CREATED_AT",
    "COMPOSE_PROJECT_NAME",
    "PROJECT_DIRECTORY",
    "COMPOSE_FILE_ORDER",
    "NON_SECRET_COMPOSE_SHA256",
    "SECRETS_OVERRIDE",
    "ENVIRONMENT_SOURCE_CLASS",
    "APPLICATION_SERVICE",
    "CANONICAL_DB_SOURCE",
    "CANONICAL_DB_TARGET",
    "CURRENT_PRODUCTION_IMAGE_ID",
    "TARGET_IMAGE_ID",
    "SOURCE_SHA",
    "TREE_SHA",
    "CONTAINS_SECRET_VALUES",
})

DIRECTORY_IDENTITY_FIELDS = frozenset({"PATH", "DEVICE", "INODE", "UID", "GID", "MODE"})
SECRET_OVERRIDE_FIELDS = frozenset({
    "PATH",
    "DEVICE",
    "INODE",
    "SIZE",
    "UID",
    "GID",
    "MODE",
    "SHA256",
})


class ExecutionAuthorityError(RuntimeError):
    """A final execution-authority check failed closed."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _effective_identity() -> tuple[int, int]:
    getters = (
        getattr(os, "geteuid", None),
        getattr(os, "getegid", None),
    )
    if not all(callable(getter) for getter in getters):
        raise ExecutionAuthorityError("POSIX_IDENTITY_REQUIRED")
    return int(getters[0]()), int(getters[1]())


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")


def _absolute_path(value: object, code: str) -> Path:
    if not isinstance(value, str):
        raise ExecutionAuthorityError(code)
    path = Path(value)
    if not path.is_absolute() or Path(os.path.normpath(value)) != path:
        raise ExecutionAuthorityError(code)
    return path


def _timestamp(value: object, code: str) -> datetime:
    if not isinstance(value, str):
        raise ExecutionAuthorityError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionAuthorityError(code) from exc
    if parsed.tzinfo is None:
        raise ExecutionAuthorityError(code)
    return parsed.astimezone(timezone.utc)


def _no_symlink_chain(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ExecutionAuthorityError(
                "AUTHORITY_PATH_METADATA_UNAVAILABLE"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ExecutionAuthorityError("AUTHORITY_SYMLINK_PATH_REFUSED")


def _read_exact(fd: int, expected_size: int, code: str) -> bytes:
    if expected_size < 0 or expected_size > MAX_ARTIFACT_BYTES:
        raise ExecutionAuthorityError(code)
    chunks: list[bytes] = []
    consumed = 0
    while consumed < expected_size:
        chunk = os.pread(fd, min(65536, expected_size - consumed), consumed)
        if not chunk:
            raise ExecutionAuthorityError(code)
        chunks.append(chunk)
        consumed += len(chunk)
    if os.pread(fd, 1, consumed):
        raise ExecutionAuthorityError(code)
    return b"".join(chunks)


@dataclass
class BoundArtifact:
    path: Path
    parent_fd: int
    file_fd: int
    identity: tuple[int, int, int, int, int, int, int]
    sha256: str
    data: bytes
    code_prefix: str

    def path_matches(self) -> bool:
        try:
            path_metadata = os.stat(
                self.path.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            descriptor_metadata = os.fstat(self.file_fd)
            data = _read_exact(
                self.file_fd,
                self.identity[2],
                f"{self.code_prefix}_READ_CONTRACT_VIOLATION",
            )
        except (OSError, ExecutionAuthorityError):
            return False
        actual = (
            path_metadata.st_dev,
            path_metadata.st_ino,
            path_metadata.st_size,
            path_metadata.st_uid,
            path_metadata.st_gid,
            stat.S_IMODE(path_metadata.st_mode),
            path_metadata.st_nlink,
        )
        descriptor = (
            descriptor_metadata.st_dev,
            descriptor_metadata.st_ino,
            descriptor_metadata.st_size,
            descriptor_metadata.st_uid,
            descriptor_metadata.st_gid,
            stat.S_IMODE(descriptor_metadata.st_mode),
            descriptor_metadata.st_nlink,
        )
        return (
            actual == self.identity
            and descriptor == self.identity
            and hashlib.sha256(data).hexdigest() == self.sha256
        )

    def close(self) -> None:
        errors: list[OSError] = []
        for fd in (self.file_fd, self.parent_fd):
            try:
                os.close(fd)
            except OSError as exc:
                errors.append(exc)
        if errors:
            raise ExecutionAuthorityError(f"{self.code_prefix}_CLOSE_FAILED")


@dataclass
class BoundJsonArtifact:
    artifact: BoundArtifact
    payload: dict[str, Any]

    @property
    def path(self) -> Path:
        return self.artifact.path

    @property
    def sha256(self) -> str:
        return self.artifact.sha256

    def path_matches(self) -> bool:
        return self.artifact.path_matches()

    def close(self) -> None:
        self.artifact.close()


def _open_bound_artifact(
    path_value: object,
    expected_sha: object,
    *,
    code_prefix: str,
    expected_mode: int = 0o600,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> BoundArtifact:
    path = _absolute_path(path_value, f"{code_prefix}_PATH_INVALID")
    if not isinstance(expected_sha, str) or SHA_RE.fullmatch(expected_sha) is None:
        raise ExecutionAuthorityError(f"{code_prefix}_SHA256_INVALID")
    _no_symlink_chain(path)
    current_uid, current_gid = _effective_identity()
    expected_uid = current_uid if expected_uid is None else expected_uid
    expected_gid = current_gid if expected_gid is None else expected_gid
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        file_fd = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except Exception:
        os.close(parent_fd)
        raise
    try:
        parent_metadata = os.fstat(parent_fd)
        metadata = os.fstat(file_fd)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != expected_uid
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != expected_uid
            or metadata.st_gid != expected_gid
            or stat.S_IMODE(metadata.st_mode) != expected_mode
            or metadata.st_size > MAX_ARTIFACT_BYTES
        ):
            raise ExecutionAuthorityError(f"{code_prefix}_METADATA_INVALID")
        data = _read_exact(
            file_fd,
            metadata.st_size,
            f"{code_prefix}_READ_CONTRACT_VIOLATION",
        )
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != expected_sha:
            raise ExecutionAuthorityError(f"{code_prefix}_SHA256_MISMATCH")
        path_metadata = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_uid,
            metadata.st_gid,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_nlink,
        )
        if (
            path_metadata.st_dev,
            path_metadata.st_ino,
            path_metadata.st_size,
            path_metadata.st_uid,
            path_metadata.st_gid,
            stat.S_IMODE(path_metadata.st_mode),
            path_metadata.st_nlink,
        ) != identity:
            raise ExecutionAuthorityError(f"{code_prefix}_PATH_SUBSTITUTION")
        return BoundArtifact(
            path=path,
            parent_fd=parent_fd,
            file_fd=file_fd,
            identity=identity,
            sha256=actual_sha,
            data=data,
            code_prefix=code_prefix,
        )
    except Exception:
        os.close(file_fd)
        os.close(parent_fd)
        raise


def _open_bound_json(
    path_value: object,
    expected_sha: object,
    *,
    code_prefix: str,
    fields: frozenset[str],
) -> BoundJsonArtifact:
    artifact = _open_bound_artifact(
        path_value,
        expected_sha,
        code_prefix=code_prefix,
    )
    try:
        try:
            payload = json.loads(artifact.data.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ExecutionAuthorityError(f"{code_prefix}_JSON_INVALID") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != fields
            or _canonical_json(payload) != artifact.data
        ):
            raise ExecutionAuthorityError(f"{code_prefix}_CONTRACT_INVALID")
        return BoundJsonArtifact(artifact=artifact, payload=payload)
    except Exception:
        try:
            artifact.close()
        except ExecutionAuthorityError:
            pass
        raise


def _git(*arguments: str, root: Path, binary: bool = False) -> str | bytes:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=not binary,
        check=False,
        env=environment,
    )
    if result.returncode != 0:
        raise ExecutionAuthorityError("OPERATIONS_ROOT_GIT_PROVENANCE_INVALID")
    return result.stdout


def _blob_oid(data: bytes, object_format: str) -> str:
    if object_format not in {"sha1", "sha256"}:
        raise ExecutionAuthorityError("OPERATIONS_ROOT_OBJECT_FORMAT_UNSUPPORTED")
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def _read_repository_file(path: Path, expected_size: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if before.st_size != expected_size:
            raise ExecutionAuthorityError("OPERATIONS_ROOT_FILE_SIZE_DRIFT")
        chunks: list[bytes] = []
        consumed = 0
        while consumed < expected_size:
            chunk = os.pread(fd, min(1024 * 1024, expected_size - consumed), consumed)
            if not chunk:
                raise ExecutionAuthorityError("OPERATIONS_ROOT_PREMATURE_EOF")
            chunks.append(chunk)
            consumed += len(chunk)
        if os.pread(fd, 1, consumed):
            raise ExecutionAuthorityError("OPERATIONS_ROOT_SIZE_CONTRACT_VIOLATION")
        after = os.fstat(fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mode,
            before.st_nlink,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mode,
            after.st_nlink,
        ):
            raise ExecutionAuthorityError("OPERATIONS_ROOT_FILE_CHANGED_DURING_READ")
        return b"".join(chunks)
    finally:
        os.close(fd)


def exact_repository_provenance(repository_root: Path) -> tuple[str, str]:
    """Return exact HEAD/tree only when filesystem closure equals the Git tree."""

    _no_symlink_chain(repository_root)
    current_uid, current_gid = _effective_identity()
    root_metadata = repository_root.lstat()
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_uid != current_uid
        or root_metadata.st_gid != current_gid
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
    ):
        raise ExecutionAuthorityError("OPERATIONS_ROOT_METADATA_INVALID")
    head = str(_git("rev-parse", "--verify", "HEAD", root=repository_root)).strip()
    tree = str(
        _git("rev-parse", "--verify", "HEAD^{tree}", root=repository_root)
    ).strip()
    object_format = str(
        _git("rev-parse", "--show-object-format", root=repository_root)
    ).strip()
    alternate_value = str(
        _git("rev-parse", "--git-path", "objects/info/alternates", root=repository_root)
    ).strip()
    alternates = Path(alternate_value)
    if not alternates.is_absolute():
        alternates = repository_root / alternates
    if alternates.exists() and alternates.stat().st_size:
        raise ExecutionAuthorityError("OPERATIONS_ROOT_OBJECT_ALTERNATES_DENIED")

    raw = bytes(
        _git("ls-tree", "-rz", "--full-tree", "HEAD", root=repository_root, binary=True)
    )
    tracked: dict[str, tuple[str, str]] = {}
    expected_paths: set[str] = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        header, raw_path = record.split(b"\t", 1)
        mode, kind, oid = header.decode("ascii").split(" ")
        relative = raw_path.decode("utf-8", "surrogateescape")
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or kind != "blob":
            raise ExecutionAuthorityError("OPERATIONS_ROOT_TREE_ENTRY_UNSUPPORTED")
        if mode not in {"100644", "100755"}:
            raise ExecutionAuthorityError("OPERATIONS_ROOT_TREE_MODE_UNSUPPORTED")
        tracked[relative] = (mode, oid)
        expected_paths.add(relative)
        parent = path.parent
        while parent != Path("."):
            expected_paths.add(parent.as_posix())
            parent = parent.parent

    actual_paths: set[str] = set()

    def inventory(directory: Path, prefix: Path) -> None:
        for entry in os.scandir(directory):
            if not prefix.parts and entry.name == ".git":
                continue
            relative_path = prefix / entry.name
            relative = relative_path.as_posix()
            metadata = entry.stat(follow_symlinks=False)
            actual_paths.add(relative)
            if stat.S_ISDIR(metadata.st_mode):
                inventory(Path(entry.path), relative_path)
            elif not stat.S_ISREG(metadata.st_mode):
                raise ExecutionAuthorityError("OPERATIONS_ROOT_SPECIAL_FILE_DENIED")

    inventory(repository_root, Path())
    if actual_paths != expected_paths:
        raise ExecutionAuthorityError("OPERATIONS_ROOT_FILESYSTEM_CLOSURE_DRIFT")

    for relative, (mode, oid) in tracked.items():
        path = repository_root / relative
        metadata = path.lstat()
        expected_mode = 0o755 if mode == "100755" else 0o644
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != root_metadata.st_uid
            or metadata.st_gid != root_metadata.st_gid
            or stat.S_IMODE(metadata.st_mode) != expected_mode
        ):
            raise ExecutionAuthorityError("OPERATIONS_ROOT_FILE_METADATA_DRIFT")
        data = _read_repository_file(path, metadata.st_size)
        if _blob_oid(data, object_format) != oid:
            raise ExecutionAuthorityError("OPERATIONS_ROOT_FILE_CONTENT_DRIFT")
    return head, tree


def _typed_fields(payload: dict[str, Any], expected: dict[str, Any], code: str) -> None:
    if any(
        type(payload.get(name)) is not type(value) or payload.get(name) != value
        for name, value in expected.items()
    ):
        raise ExecutionAuthorityError(code)


def _inspect_image(image_id: str, revision: str | None) -> None:
    if IMAGE_ID_RE.fullmatch(image_id) is None:
        raise ExecutionAuthorityError("AUTHORITY_IMAGE_ID_INVALID")
    result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            image_id,
            "--format",
            '{{.Id}}\n{{ index .Config.Labels "org.opencontainers.image.revision" }}',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = result.stdout.splitlines()
    if result.returncode != 0 or not lines or lines[0] != image_id:
        raise ExecutionAuthorityError("AUTHORITY_IMAGE_IDENTITY_DRIFT")
    if revision is not None and (len(lines) < 2 or lines[1] != revision):
        raise ExecutionAuthorityError("AUTHORITY_IMAGE_REVISION_DRIFT")


def _inspect_runtime(service_name: str) -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "inspect", service_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ExecutionAuthorityError("CURRENT_RUNTIME_UNAVAILABLE")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExecutionAuthorityError("CURRENT_RUNTIME_RESPONSE_INVALID") from exc
    if (
        not isinstance(payload, list)
        or len(payload) != 1
        or not isinstance(payload[0], dict)
    ):
        raise ExecutionAuthorityError("CURRENT_RUNTIME_RESPONSE_INVALID")
    return payload[0]


def _validate_override(artifact: BoundArtifact, source: str, target: str) -> None:
    try:
        payload = json.loads(artifact.data.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ExecutionAuthorityError("DB_OVERRIDE_JSON_INVALID") from exc
    expected = {
        "services": {
            "hermes-bot": {
                "volumes": [
                    {
                        "bind": {"create_host_path": True},
                        "source": source,
                        "target": target,
                        "type": "bind",
                    }
                ]
            }
        }
    }
    if payload != expected or _canonical_json(payload) != artifact.data:
        raise ExecutionAuthorityError("DB_OVERRIDE_CONTRACT_INVALID")


@dataclass
class ExecutionAuthorityBundle:
    final_authority: BoundJsonArtifact
    approval_envelope: BoundJsonArtifact
    invocation_descriptor: BoundJsonArtifact
    bound_files: tuple[BoundArtifact, ...]
    runtime_image_id: str

    def path_matches(self) -> bool:
        return (
            self.final_authority.path_matches()
            and self.approval_envelope.path_matches()
            and self.invocation_descriptor.path_matches()
            and all(item.path_matches() for item in self.bound_files)
        )

    def validate_source(
        self,
        *,
        identity: dict[str, int | str],
        schema_fingerprint: str,
        parent_identity: dict[str, int | str],
    ) -> None:
        authority = self.final_authority.payload
        expected = {
            "SOURCE_DB_SHA256": identity["SOURCE_SHA256"],
            "SOURCE_DB_SIZE": identity["SOURCE_SIZE"],
            "SOURCE_DB_USER_VERSION": identity["SOURCE_USER_VERSION"],
            "SOURCE_DB_SCHEMA_FINGERPRINT": schema_fingerprint,
            "SOURCE_DB_PARENT_IDENTITY": parent_identity,
        }
        _typed_fields(authority, expected, "EXECUTION_AUTHORITY_SOURCE_DRIFT")
        envelope = self.approval_envelope.payload
        _typed_fields(
            envelope,
            {
                "CANONICAL_DB_DEVICE": identity["SOURCE_DEVICE"],
                "CANONICAL_DB_INODE": identity["SOURCE_INODE"],
                "CANONICAL_DB_SIZE": identity["SOURCE_SIZE"],
                "CANONICAL_DB_SHA256": identity["SOURCE_SHA256"],
            },
            "APPROVAL_ENVELOPE_SOURCE_DRIFT",
        )

    def runtime_matches(self) -> bool:
        descriptor = self.invocation_descriptor.payload
        runtime = _inspect_runtime(str(descriptor["APPLICATION_SERVICE"]))
        return (
            bool(runtime.get("State", {}).get("Running"))
            and runtime.get("Image") == self.runtime_image_id
        )

    def close(self) -> None:
        errors: list[str] = []
        for item in (
            *reversed(self.bound_files),
            self.invocation_descriptor,
            self.approval_envelope,
            self.final_authority,
        ):
            try:
                item.close()
            except ExecutionAuthorityError as exc:
                errors.append(exc.code)
        if errors:
            raise ExecutionAuthorityError("EXECUTION_AUTHORITY_CLOSE_FAILED")


def load_execution_authority(
    *,
    authority_path: str,
    authority_sha256: str,
    plan_path: Path,
    plan_sha256: str,
    plan: dict[str, Any],
    repository_root: Path,
) -> ExecutionAuthorityBundle:
    """Load every execution input from top-level authority and validate it."""

    final = _open_bound_json(
        authority_path,
        authority_sha256,
        code_prefix="FINAL_AUTHORITY",
        fields=EXECUTION_AUTHORITY_FIELDS,
    )
    opened: list[BoundArtifact | BoundJsonArtifact] = []
    try:
        authority = final.payload
        artifact_path_fields = (
            "PLAN_PATH",
            "OPERATIONS_ROOT_APPROVAL_PATH",
            "CLEAN_START_POLICY_PATH",
            "APPROVAL_ENVELOPE_PATH",
            "INVOCATION_DESCRIPTOR_PATH",
            "PERSISTENT_DB_OVERRIDE_PATH",
            "P5B_EVIDENCE_PATH",
            "P6A_F1_EVIDENCE_PATH",
        )
        artifact_paths = tuple(
            _absolute_path(authority[name], "AUTHORITY_ARTIFACT_PATH_INVALID")
            for name in artifact_path_fields
        )
        if len(set(artifact_paths)) != len(artifact_paths):
            raise ExecutionAuthorityError("AUTHORITY_ARTIFACT_PATH_COLLISION")
        for artifact_path in artifact_paths:
            try:
                artifact_path.relative_to(repository_root)
            except ValueError:
                continue
            raise ExecutionAuthorityError(
                "AUTHORITY_ARTIFACT_INSIDE_OPERATIONS_ROOT_DENIED"
            )
        created = _timestamp(
            authority["CREATED_AT"], "FINAL_AUTHORITY_CREATED_AT_INVALID"
        )
        expires = _timestamp(
            authority["EXPIRES_AT"], "FINAL_AUTHORITY_EXPIRES_AT_INVALID"
        )
        now = datetime.now(timezone.utc)
        if (
            created > now
            or expires <= created
            or expires - created > timedelta(days=1)
            or now >= expires
        ):
            raise ExecutionAuthorityError("FINAL_AUTHORITY_EXPIRED")
        _typed_fields(
            authority,
            {
                "EXECUTION_AUTHORITY_VERSION": EXECUTION_AUTHORITY_VERSION,
                "PLAN_PATH": str(plan_path),
                "PLAN_SHA256": plan_sha256,
                "OPERATIONS_ROOT_APPROVAL_PATH": plan["OPERATIONS_ROOT_APPROVAL_PATH"],
                "OPERATIONS_ROOT_APPROVAL_SHA256": plan[
                    "OPERATIONS_ROOT_APPROVAL_SHA256"
                ],
                "CLEAN_START_POLICY_PATH": plan["CLEAN_START_POLICY_PATH"],
                "CLEAN_START_POLICY_SHA256": plan["CLEAN_START_POLICY_SHA256"],
                "SOURCE_SHA": plan["MIGRATION_IMAGE_REVISION"],
                "TARGET_IMAGE_ID": plan["MIGRATION_IMAGE_ID"],
                "CURRENT_RUNTIME_IMAGE_ID": plan["PREVIOUS_IMAGE_ID"],
                "CANONICAL_PRODUCTION_DB_PATH": plan["DB_CANONICAL_PATH"],
                "SOURCE_DB_SHA256": plan["SOURCE_SHA256"],
                "SOURCE_DB_SIZE": plan["SOURCE_SIZE"],
                "SOURCE_DB_USER_VERSION": plan["SOURCE_USER_VERSION"],
                "SOURCE_DB_SCHEMA_FINGERPRINT": plan["SOURCE_SCHEMA_FINGERPRINT"],
                "SOURCE_DB_PARENT_IDENTITY": plan["SOURCE_PARENT_IDENTITY"],
                "OPERATIONS_ROOT_PATH": str(repository_root),
                "EXECUTION_AUTHORIZED": True,
                "DEPLOY_AUTHORIZED": False,
                "CONTAINS_SECRETS": False,
            },
            "FINAL_AUTHORITY_PLAN_BINDING_MISMATCH",
        )
        if authority["SOURCE_TREE_SHA"] != authority["OPERATIONS_ROOT_TREE_SHA"]:
            raise ExecutionAuthorityError("FINAL_AUTHORITY_TREE_BINDING_MISMATCH")
        if authority["OPERATIONS_ROOT_HEAD_SHA"] != authority["SOURCE_SHA"]:
            raise ExecutionAuthorityError("FINAL_AUTHORITY_HEAD_BINDING_MISMATCH")
        head, tree = exact_repository_provenance(repository_root)
        current_uid, current_gid = _effective_identity()
        if (
            head != authority["OPERATIONS_ROOT_HEAD_SHA"]
            or tree != authority["OPERATIONS_ROOT_TREE_SHA"]
        ):
            raise ExecutionAuthorityError("OPERATIONS_ROOT_AUTHORITY_DRIFT")

        envelope = _open_bound_json(
            authority["APPROVAL_ENVELOPE_PATH"],
            authority["APPROVAL_ENVELOPE_SHA256"],
            code_prefix="APPROVAL_ENVELOPE",
            fields=APPROVAL_ENVELOPE_FIELDS,
        )
        opened.append(envelope)
        descriptor = _open_bound_json(
            authority["INVOCATION_DESCRIPTOR_PATH"],
            authority["INVOCATION_DESCRIPTOR_SHA256"],
            code_prefix="INVOCATION_DESCRIPTOR",
            fields=INVOCATION_DESCRIPTOR_FIELDS,
        )
        opened.append(descriptor)
        p5b = _open_bound_artifact(
            authority["P5B_EVIDENCE_PATH"],
            authority["P5B_EVIDENCE_SHA256"],
            code_prefix="P5B_EVIDENCE",
        )
        opened.append(p5b)
        p6a_f1 = _open_bound_artifact(
            authority["P6A_F1_EVIDENCE_PATH"],
            authority["P6A_F1_EVIDENCE_SHA256"],
            code_prefix="P6A_F1_EVIDENCE",
        )
        opened.append(p6a_f1)

        env = envelope.payload
        _typed_fields(
            env,
            {
                "ENVELOPE_VERSION": 1,
                "PUBLIC_OPERATIONS_ROOT_APPROVAL_PATH": authority[
                    "OPERATIONS_ROOT_APPROVAL_PATH"
                ],
                "PUBLIC_OPERATIONS_ROOT_APPROVAL_SHA256": authority[
                    "OPERATIONS_ROOT_APPROVAL_SHA256"
                ],
                "OPERATIONS_ROOT_PATH": str(repository_root),
                "OPERATIONS_ROOT_HEAD_SHA": head,
                "OPERATIONS_ROOT_TREE_SHA": tree,
                "OPERATIONS_ROOT_MODE": 0o700,
                "OPERATIONS_ROOT_UID": current_uid,
                "OPERATIONS_ROOT_GID": current_gid,
                "OPERATIONS_ROOT_CLEAN": True,
                "OBJECT_ALTERNATES_ABSENT": True,
                "P5B_EVIDENCE_SHA256": p5b.sha256,
                "P6A_F1_EVIDENCE_SHA256": p6a_f1.sha256,
                "EXACT_MAIN_IMAGE_ID": plan["MIGRATION_IMAGE_ID"],
                "CANONICAL_DB_PATH": plan["DB_CANONICAL_PATH"],
                "PERSISTENT_DB_OVERRIDE_SHA256": authority[
                    "PERSISTENT_DB_OVERRIDE_SHA256"
                ],
                "INVOCATION_DESCRIPTOR_SHA256": descriptor.sha256,
                "CLEAN_START_POLICY_SHA256": authority["CLEAN_START_POLICY_SHA256"],
                "PLAN_ONLY_AUTHORIZED": True,
                "EXECUTION_AUTHORIZED": False,
                "DEPLOY_AUTHORIZED": False,
                "CONTAINS_SECRETS": False,
            },
            "APPROVAL_ENVELOPE_BINDING_MISMATCH",
        )

        desc = descriptor.payload
        _typed_fields(
            desc,
            {
                "DESCRIPTOR_VERSION": INVOCATION_DESCRIPTOR_VERSION,
                "PROJECT_DIRECTORY": str(repository_root),
                "APPLICATION_SERVICE": "hermes-bot",
                "CANONICAL_DB_SOURCE": plan["DB_CANONICAL_PATH"],
                "CANONICAL_DB_TARGET": "/home/hermes/healbite.db",
                "CURRENT_PRODUCTION_IMAGE_ID": plan["PREVIOUS_IMAGE_ID"],
                "TARGET_IMAGE_ID": plan["MIGRATION_IMAGE_ID"],
                "SOURCE_SHA": head,
                "TREE_SHA": tree,
                "CONTAINS_SECRET_VALUES": False,
            },
            "INVOCATION_DESCRIPTOR_BINDING_MISMATCH",
        )
        _timestamp(desc["CREATED_AT"], "INVOCATION_DESCRIPTOR_CREATED_AT_INVALID")
        if (
            not isinstance(desc["COMPOSE_PROJECT_NAME"], str)
            or not desc["COMPOSE_PROJECT_NAME"]
        ):
            raise ExecutionAuthorityError("INVOCATION_DESCRIPTOR_PROJECT_INVALID")
        if (
            desc["ENVIRONMENT_SOURCE_CLASS"]
            != "EXISTING_PRODUCTION_ENV_FILE_METADATA_ONLY"
        ):
            raise ExecutionAuthorityError("INVOCATION_DESCRIPTOR_ENVIRONMENT_INVALID")
        order = desc["COMPOSE_FILE_ORDER"]
        secrets = desc["SECRETS_OVERRIDE"]
        non_secret = desc["NON_SECRET_COMPOSE_SHA256"]
        if (
            not isinstance(order, list)
            or len(order) != 3
            or not all(isinstance(x, str) for x in order)
        ):
            raise ExecutionAuthorityError("COMPOSE_FILE_ORDER_INVALID")
        if not isinstance(secrets, dict) or set(secrets) != SECRET_OVERRIDE_FIELDS:
            raise ExecutionAuthorityError("SECRETS_OVERRIDE_IDENTITY_INVALID")
        expected_order = [
            str(repository_root / "docker-compose.yml"),
            authority["PERSISTENT_DB_OVERRIDE_PATH"],
            secrets["PATH"],
        ]
        if order != expected_order:
            raise ExecutionAuthorityError("COMPOSE_FILE_ORDER_DRIFT")
        override_path = _absolute_path(order[1], "DB_OVERRIDE_PATH_INVALID")
        try:
            override_path.relative_to(repository_root)
        except ValueError:
            pass
        else:
            raise ExecutionAuthorityError("DB_OVERRIDE_INSIDE_OPERATIONS_ROOT_DENIED")
        if not isinstance(non_secret, dict) or set(non_secret) != set(order[:2]):
            raise ExecutionAuthorityError("NON_SECRET_COMPOSE_BINDINGS_INVALID")
        base_compose = _open_bound_artifact(
            order[0],
            non_secret[order[0]],
            code_prefix="BASE_COMPOSE",
            expected_mode=0o644,
        )
        opened.append(base_compose)
        if non_secret[order[1]] != authority["PERSISTENT_DB_OVERRIDE_SHA256"]:
            raise ExecutionAuthorityError("NON_SECRET_COMPOSE_SHA256_MISMATCH")

        override = _open_bound_artifact(
            authority["PERSISTENT_DB_OVERRIDE_PATH"],
            authority["PERSISTENT_DB_OVERRIDE_SHA256"],
            code_prefix="DB_OVERRIDE",
        )
        opened.append(override)
        secret = _open_bound_artifact(
            secrets["PATH"],
            secrets["SHA256"],
            code_prefix="SECRETS_OVERRIDE",
            expected_mode=secrets["MODE"],
            expected_uid=secrets["UID"],
            expected_gid=secrets["GID"],
        )
        opened.append(secret)
        if (
            secret.identity[0] != secrets["DEVICE"]
            or secret.identity[1] != secrets["INODE"]
            or secret.identity[2] != secrets["SIZE"]
        ):
            raise ExecutionAuthorityError("SECRETS_OVERRIDE_IDENTITY_DRIFT")
        _validate_override(
            override,
            str(plan["DB_CANONICAL_PATH"]),
            str(desc["CANONICAL_DB_TARGET"]),
        )

        target_image = str(plan["MIGRATION_IMAGE_ID"])
        current_image = str(plan["PREVIOUS_IMAGE_ID"])
        _inspect_image(target_image, str(plan["MIGRATION_IMAGE_REVISION"]))
        _inspect_image(current_image, None)
        runtime = _inspect_runtime(str(desc["APPLICATION_SERVICE"]))
        mounts = [
            item
            for item in runtime.get("Mounts", [])
            if item.get("Destination") == desc["CANONICAL_DB_TARGET"]
        ]
        if (
            not runtime.get("State", {}).get("Running")
            or runtime.get("Image") != current_image
            or len(mounts) != 1
            or mounts[0].get("Source") != plan["DB_CANONICAL_PATH"]
        ):
            raise ExecutionAuthorityError("CURRENT_RUNTIME_IDENTITY_DRIFT")

        return ExecutionAuthorityBundle(
            final_authority=final,
            approval_envelope=envelope,
            invocation_descriptor=descriptor,
            bound_files=(p5b, p6a_f1, base_compose, override, secret),
            runtime_image_id=current_image,
        )
    except Exception:
        for item in reversed(opened):
            try:
                item.close()
            except ExecutionAuthorityError:
                pass
        try:
            final.close()
        except ExecutionAuthorityError:
            pass
        raise
