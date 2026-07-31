#!/usr/bin/env python3
"""Materialize a reviewed Playwright closure on a trusted remote builder."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

from scripts import install_pinned_playwright_artifact as installer
from scripts.playwright_artifact_contract import (
    PlaywrightContractError,
    VerifiedPlaywrightClosure,
    load_locked_wheel,
    load_verified_wheel_closure,
)


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_DOWNLOAD_HOSTS = frozenset({
    "cdn.playwright.dev",
    "files.pythonhosted.org",
    "playwright.download.prss.microsoft.com",
    "storage.googleapis.com",
})
_POLICY_KEYS = frozenset({
    "approved_base_sha",
    "artifacts",
    "closure_manifest_sha256",
    "platform",
    "policy_version",
    "wheel_url",
})
_ARTIFACT_KEYS = frozenset({
    "archive_sha256",
    "archive_size",
    "artifact_name",
    "source_kind",
    "source_reference_sha256",
    "url",
})
_MAX_POLICY_BYTES = 64 * 1024


class RemoteArtifactError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DuplicateJsonKey(ValueError):
    pass


def _fail(code: str) -> None:
    raise RemoteArtifactError(code)


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _read_policy(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= _MAX_POLICY_BYTES
        ):
            _fail("REMOTE_ARTIFACT_POLICY_INVALID")
        data = path.read_bytes()
        document = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except RemoteArtifactError:
        raise
    except (_DuplicateJsonKey, OSError, UnicodeError, json.JSONDecodeError):
        _fail("REMOTE_ARTIFACT_POLICY_INVALID")
    if not isinstance(document, dict) or set(document) != _POLICY_KEYS:
        _fail("REMOTE_ARTIFACT_POLICY_INVALID")
    if document["policy_version"] != 1 or document["platform"] != "linux/amd64":
        _fail("REMOTE_ARTIFACT_POLICY_INVALID")
    if (
        not isinstance(document["approved_base_sha"], str)
        or _SHA_RE.fullmatch(document["approved_base_sha"]) is None
        or not isinstance(document["closure_manifest_sha256"], str)
        or _SHA256_RE.fullmatch(document["closure_manifest_sha256"]) is None
        or not isinstance(document["wheel_url"], str)
    ):
        _fail("REMOTE_ARTIFACT_POLICY_INVALID")
    artifacts = document["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        _fail("REMOTE_ARTIFACT_POLICY_INVALID")
    names: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != _ARTIFACT_KEYS:
            _fail("REMOTE_ARTIFACT_POLICY_INVALID")
        name = item["artifact_name"]
        if not isinstance(name, str) or not name or name in names:
            _fail("REMOTE_ARTIFACT_POLICY_INVALID")
        names.add(name)
        if (
            type(item["archive_size"]) is not int
            or not 0 < item["archive_size"] < 1024 * 1024 * 1024
            or not isinstance(item["archive_sha256"], str)
            or _SHA256_RE.fullmatch(item["archive_sha256"]) is None
            or item["source_kind"] != "operator-approved-offline-artifact"
            or not isinstance(item["source_reference_sha256"], str)
            or _SHA256_RE.fullmatch(item["source_reference_sha256"]) is None
            or not isinstance(item["url"], str)
        ):
            _fail("REMOTE_ARTIFACT_POLICY_INVALID")
    return document


def _validate_url(url: str) -> urllib.parse.SplitResult:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        _fail("REMOTE_ARTIFACT_URL_DENIED")
    return parsed


class _ApprovedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(
    *,
    url: str,
    destination: Path,
    expected_size: int,
    expected_sha256: str,
) -> None:
    _validate_url(url)
    opener = urllib.request.build_opener(_ApprovedRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "hermes-reviewed-artifact-acquisition/1"},
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with opener.open(request, timeout=120) as response:
            _validate_url(response.geturl())
            if getattr(response, "status", 200) != 200:
                _fail("REMOTE_ARTIFACT_DOWNLOAD_FAILED")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) != expected_size:
                _fail("REMOTE_ARTIFACT_SIZE_MISMATCH")
            with destination.open("xb") as handle:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > expected_size:
                        _fail("REMOTE_ARTIFACT_SIZE_MISMATCH")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
    except RemoteArtifactError:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        _fail("REMOTE_ARTIFACT_DOWNLOAD_FAILED")
    if total != expected_size:
        _fail("REMOTE_ARTIFACT_SIZE_MISMATCH")
    if digest.hexdigest() != expected_sha256:
        _fail("REMOTE_ARTIFACT_SHA256_MISMATCH")
    destination.chmod(0o600)


def _locked_wheel_url(lockfile: Path, filename: str) -> str:
    try:
        document = tomllib.loads(lockfile.read_text(encoding="utf-8"))
        packages = document["package"]
        package = [
            item for item in packages
            if item.get("name") == "playwright"
        ]
        if len(package) != 1:
            _fail("REMOTE_ARTIFACT_LOCK_INVALID")
        matches = [
            item["url"] for item in package[0]["wheels"]
            if item["url"].rsplit("/", 1)[-1] == filename
        ]
    except RemoteArtifactError:
        raise
    except (KeyError, OSError, TypeError, UnicodeError, tomllib.TOMLDecodeError):
        _fail("REMOTE_ARTIFACT_LOCK_INVALID")
    if len(matches) != 1 or not isinstance(matches[0], str):
        _fail("REMOTE_ARTIFACT_LOCK_INVALID")
    return matches[0]


def _manifest_document(
    verified: VerifiedPlaywrightClosure,
    policy_artifacts: dict[str, dict[str, object]],
) -> dict[str, object]:
    artifacts: list[dict[str, object]] = []
    for artifact in verified.closure.artifacts:
        approved = policy_artifacts.get(artifact.artifact_name)
        if approved is None:
            _fail("REMOTE_ARTIFACT_SET_MISMATCH")
        artifacts.append({
            "archive_filename": artifact.expected_archive_filename,
            "archive_format": "zip",
            "archive_root": artifact.archive_root,
            "archive_sha256": approved["archive_sha256"],
            "archive_size": approved["archive_size"],
            "artifact_name": artifact.artifact_name,
            "browser_family": artifact.browser_family,
            "browser_version": artifact.browser_version,
            "executable_mode_required": True,
            "expected_executable_relative_path": (
                artifact.expected_executable_relative_path
            ),
            "layout_kind": artifact.layout_kind,
            "platform": artifact.platform,
            "revision": artifact.revision,
            "source_kind": approved["source_kind"],
            "source_reference_sha256": approved["source_reference_sha256"],
        })
    return {
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "cache_root": verified.closure.cache_root,
        "manifest_kind": installer.MANIFEST_KIND,
        "manifest_version": installer.MANIFEST_VERSION,
        "platform": verified.closure.platform,
        "playwright_package": verified.closure.package,
        "playwright_package_version": verified.closure.package_version,
        "playwright_wheel_filename": verified.wheel.filename,
        "playwright_wheel_sha256": verified.wheel.sha256,
        "playwright_wheel_size": verified.wheel.size,
    }


def prepare(*, policy_path: Path, lockfile: Path, output: Path) -> str:
    policy = _read_policy(policy_path)
    if not output.is_absolute() or output.exists() or not output.parent.is_dir():
        _fail("REMOTE_ARTIFACT_OUTPUT_INVALID")
    wheel = load_locked_wheel(lockfile, str(policy["platform"]))
    wheel_url = _locked_wheel_url(lockfile, wheel.filename)
    if wheel_url != policy["wheel_url"]:
        _fail("REMOTE_ARTIFACT_WHEEL_URL_MISMATCH")
    _validate_url(wheel_url)
    try:
        output.mkdir(mode=0o700)
        wheel_path = output / "playwright-wheel"
        _download(
            url=wheel_url,
            destination=wheel_path,
            expected_size=wheel.size,
            expected_sha256=wheel.sha256,
        )
        verified = load_verified_wheel_closure(
            lockfile_path=lockfile,
            wheel_path=wheel_path,
            platform=str(policy["platform"]),
        )
        approved = {
            str(item["artifact_name"]): item
            for item in policy["artifacts"]
        }
        if set(approved) != set(verified.closure.artifact_names):
            _fail("REMOTE_ARTIFACT_SET_MISMATCH")
        artifacts_root = output / "artifacts"
        artifacts_root.mkdir(mode=0o700)
        for artifact in verified.closure.artifacts:
            item = approved[artifact.artifact_name]
            directory = artifacts_root / artifact.artifact_name
            directory.mkdir(mode=0o700)
            _download(
                url=str(item["url"]),
                destination=directory / "archive",
                expected_size=int(item["archive_size"]),
                expected_sha256=str(item["archive_sha256"]),
            )
        manifest_data = installer.canonical_json(
            _manifest_document(verified, approved)
        )
        manifest_sha = hashlib.sha256(manifest_data).hexdigest()
        if manifest_sha != policy["closure_manifest_sha256"]:
            _fail("REMOTE_ARTIFACT_CLOSURE_IDENTITY_MISMATCH")
        manifest_path = output / "closure.json"
        with manifest_path.open("xb") as handle:
            handle.write(manifest_data)
            handle.flush()
            os.fsync(handle.fileno())
        manifest_path.chmod(0o600)
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return str(policy["closure_manifest_sha256"])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize the reviewed Playwright remote-build closure."
    )
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--lockfile", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        digest = prepare(
            policy_path=args.policy,
            lockfile=args.lockfile,
            output=args.output,
        )
        print("REMOTE_PLAYWRIGHT_ARTIFACTS=PASS")
        print(f"CLOSURE_MANIFEST_SHA256={digest}")
        print("ARTIFACT_COUNT=2")
        print("SECRET_VALUES_OUTPUT=false")
    except (RemoteArtifactError, PlaywrightContractError) as exc:
        print("REMOTE_PLAYWRIGHT_ARTIFACTS=FAIL", file=sys.stderr)
        print(f"ERROR_CLASS={exc.code}", file=sys.stderr)
        return 1
    except Exception:
        print("REMOTE_PLAYWRIGHT_ARTIFACTS=FAIL", file=sys.stderr)
        print("ERROR_CLASS=INTERNAL_ERROR", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
