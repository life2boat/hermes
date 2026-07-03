
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

_ALLOWED_ACTIONS = frozenset({"household_schema_initialize", "household_bootstrap_apply"})
_ALLOWED_KEYS = frozenset(
    {
        "schema_version",
        "action",
        "database_realpath",
        "database_device",
        "database_inode",
        "expected_revision",
        "issued_at_utc",
        "expires_at_utc",
        "nonce",
    }
)
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HEX_NONCE_RE = re.compile(r"^[0-9a-f]{32,128}$")
_URLSAFE_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
_DEFAULT_REVISION_FILE = Path("/opt/hermes/.hermes_build_sha")
_MAX_VALIDITY = timedelta(minutes=15)
_MAX_CLOCK_SKEW = timedelta(seconds=60)


class ProductionAuthorizationError(Exception):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


@dataclass(frozen=True, slots=True)
class PreparedProductionAuthorization:
    path: Path
    action: str
    database_realpath: str
    expected_revision: str
    _payload: Mapping[str, Any]
    _revision_provider: Callable[[], str]
    _now_provider: Callable[[], datetime]

    def claim(self) -> "ClaimedProductionAuthorization":
        # Re-read and revalidate immediately before claiming so a replaced file,
        # changed DB identity, expired capability, or changed runtime revision is refused.
        _validate_authorization_file(
            self.path,
            action=self.action,
            db_path=Path(self.database_realpath),
            revision_provider=self._revision_provider,
            now_provider=self._now_provider,
        )
        claimed = self.path.with_name(f"{self.path.name}.claimed")
        if claimed.exists():
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_REPLAY_REFUSED")
        try:
            os.link(self.path, claimed, follow_symlinks=False)
            os.unlink(self.path)
        except FileExistsError as exc:
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_REPLAY_REFUSED") from exc
        except OSError as exc:
            try:
                claimed.unlink()
            except OSError:
                pass
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_CLAIM_FAILED") from exc
        _validate_authorization_file(
            claimed,
            action=self.action,
            db_path=Path(self.database_realpath),
            revision_provider=self._revision_provider,
            now_provider=self._now_provider,
        )
        return ClaimedProductionAuthorization(path=claimed)


@dataclass(frozen=True, slots=True)
class ClaimedProductionAuthorization:
    path: Path

    def consume_success(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return


def get_runtime_revision() -> str:
    try:
        value = _DEFAULT_REVISION_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_REVISION_UNAVAILABLE") from exc
    if not _valid_full_sha(value):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_REVISION_UNAVAILABLE")
    return value


def prepare_production_authorization(
    authorization_file: str | Path | None,
    *,
    action: str,
    db_path: str | Path,
    revision_provider: Callable[[], str] | None = None,
    now_provider: Callable[[], datetime] | None = None,
) -> PreparedProductionAuthorization:
    if authorization_file is None:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_REQUIRED")
    provider = revision_provider or get_runtime_revision
    now = now_provider or (lambda: datetime.now(UTC))
    path = Path(authorization_file)
    if path.name.endswith(".claimed") or path.with_name(f"{path.name}.claimed").exists():
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_REPLAY_REFUSED")
    payload = _validate_authorization_file(path, action=action, db_path=Path(db_path), revision_provider=provider, now_provider=now)
    return PreparedProductionAuthorization(
        path=path,
        action=action,
        database_realpath=str(Path(db_path).resolve(strict=True)),
        expected_revision=str(payload["expected_revision"]),
        _payload=payload,
        _revision_provider=provider,
        _now_provider=now,
    )


def _valid_full_sha(value: object) -> bool:
    return isinstance(value, str) and _FULL_SHA_RE.fullmatch(value) is not None


def _parse_utc(value: object, *, error_type: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ProductionAuthorizationError(error_type)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ProductionAuthorizationError(error_type) from exc
    if parsed.tzinfo is None:
        raise ProductionAuthorizationError(error_type)
    return parsed.astimezone(UTC)


def _validate_nonce(value: object) -> None:
    if not isinstance(value, str):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID_NONCE")
    if _HEX_NONCE_RE.fullmatch(value) or _URLSAFE_NONCE_RE.fullmatch(value):
        return
    raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID_NONCE")


def _validate_file_stat(path: Path, file_stat: os.stat_result) -> None:
    if stat.S_ISLNK(file_stat.st_mode):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_FILE_INVALID")
    if not stat.S_ISREG(file_stat.st_mode):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_FILE_INVALID")
    if file_stat.st_nlink != 1:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_FILE_INVALID")
    allowed_owner_uids = {0}
    get_effective_uid = getattr(os, "geteuid", None)
    if get_effective_uid is not None:
        allowed_owner_uids.add(int(get_effective_uid()))
    if file_stat.st_uid not in allowed_owner_uids:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_FILE_INVALID")
    mode = stat.S_IMODE(file_stat.st_mode)
    if mode & ~0o600:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_FILE_INVALID")
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_FILE_INVALID")


def _load_json_no_symlink(path: Path) -> tuple[dict[str, Any], os.stat_result]:
    try:
        lstat_result = path.lstat()
    except OSError as exc:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_FILE_INVALID") from exc
    _validate_file_stat(path, lstat_result)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_FILE_INVALID") from exc
    try:
        fstat_result = os.fstat(fd)
        _validate_file_stat(path, fstat_result)
        if (lstat_result.st_dev, lstat_result.st_ino) != (fstat_result.st_dev, fstat_result.st_ino):
            raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_FILE_INVALID")
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            raw = handle.read()
    finally:
        if fd != -1:
            os.close(fd)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID_JSON") from exc
    if not isinstance(parsed, dict):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_INVALID_JSON")
    return parsed, fstat_result


def _validate_authorization_file(
    path: Path,
    *,
    action: str,
    db_path: Path,
    revision_provider: Callable[[], str],
    now_provider: Callable[[], datetime],
) -> dict[str, Any]:
    if action not in _ALLOWED_ACTIONS:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_ACTION_REFUSED")
    payload, _file_stat = _load_json_no_symlink(path)
    if set(payload) != _ALLOWED_KEYS:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_SCHEMA_INVALID")
    if payload.get("schema_version") != 1:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_SCHEMA_INVALID")
    if payload.get("action") != action:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_ACTION_REFUSED")
    expected_revision = payload.get("expected_revision")
    if not _valid_full_sha(expected_revision):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_REVISION_INVALID")
    actual_revision = revision_provider()
    if not _valid_full_sha(actual_revision):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_REVISION_UNAVAILABLE")
    if actual_revision != expected_revision:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_REVISION_MISMATCH")
    _validate_nonce(payload.get("nonce"))
    issued = _parse_utc(payload.get("issued_at_utc"), error_type="PRODUCTION_AUTHORIZATION_TIME_INVALID")
    expires = _parse_utc(payload.get("expires_at_utc"), error_type="PRODUCTION_AUTHORIZATION_TIME_INVALID")
    now = now_provider()
    if now.tzinfo is None:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_TIME_INVALID")
    now = now.astimezone(UTC)
    if issued - now > _MAX_CLOCK_SKEW:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_NOT_YET_VALID")
    if expires <= issued or expires - issued > _MAX_VALIDITY:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_TIME_INVALID")
    if now >= expires:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_EXPIRED")
    try:
        db_realpath = str(db_path.resolve(strict=True))
        db_stat = db_path.stat()
    except OSError as exc:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_DATABASE_MISMATCH") from exc
    if str(payload.get("database_realpath")) != db_realpath:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_DATABASE_MISMATCH")
    try:
        device = int(payload.get("database_device"))
        inode = int(payload.get("database_inode"))
    except (TypeError, ValueError) as exc:
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_DATABASE_MISMATCH") from exc
    if (device, inode) != (int(db_stat.st_dev), int(db_stat.st_ino)):
        raise ProductionAuthorizationError("PRODUCTION_AUTHORIZATION_DATABASE_MISMATCH")
    return payload
