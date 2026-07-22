from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import build_verified_playwright_image as build_helper
from scripts import git_object_secret_policy as policy
from tests.secret_scanner_support import (
    synthetic_assignment,
    synthetic_credential_url,
    synthetic_private_key_block,
)


SOURCE_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class CallerCase:
    case_id: str
    kind: str
    content: bytes
    expected: policy.GitObjectResult


CASES = (
    CallerCase(
        "clean_utf8_regular",
        "regular",
        b"clean candidate\n",
        policy.GitObjectResult.CLEAN,
    ),
    CallerCase(
        "ordinary_secret",
        "regular",
        synthetic_assignment().encode(),
        policy.GitObjectResult.SECRET_FOUND,
    ),
    CallerCase(
        "private_key",
        "regular",
        synthetic_private_key_block().encode(),
        policy.GitObjectResult.SECRET_FOUND,
    ),
    CallerCase(
        "credential_url",
        "regular",
        synthetic_credential_url().encode(),
        policy.GitObjectResult.SECRET_FOUND,
    ),
    CallerCase(
        "binary_without_secret",
        "regular",
        b"\x00\x01binary",
        policy.GitObjectResult.BINARY_DENIED,
    ),
    CallerCase(
        "binary_with_secret",
        "regular",
        b"\x00" + synthetic_assignment().encode(),
        policy.GitObjectResult.BINARY_DENIED,
    ),
    CallerCase(
        "invalid_utf8",
        "regular",
        b"\xff\xfeinvalid",
        policy.GitObjectResult.DECODE_DENIED,
    ),
    CallerCase(
        "empty_regular",
        "regular",
        b"",
        policy.GitObjectResult.CLEAN,
    ),
    CallerCase(
        "executable_regular",
        "executable",
        b"#!/bin/sh\nexit 0\n",
        policy.GitObjectResult.CLEAN,
    ),
    CallerCase(
        "git_symlink",
        "symlink",
        b"synthetic-target",
        policy.GitObjectResult.UNSUPPORTED_OBJECT,
    ),
    CallerCase(
        "gitlink",
        "gitlink",
        b"",
        policy.GitObjectResult.UNSUPPORTED_OBJECT,
    ),
)

_CONTEXT_RESULT_BY_CODE = {
    "GIT_SECRET_CONTENT_DENIED": policy.GitObjectResult.SECRET_FOUND,
    "GIT_BINARY_BLOB_DENIED": policy.GitObjectResult.BINARY_DENIED,
    "GIT_BLOB_UTF8_DECODE_DENIED": policy.GitObjectResult.DECODE_DENIED,
    "GIT_SYMLINK_UNSUPPORTED": policy.GitObjectResult.UNSUPPORTED_OBJECT,
    "GIT_SUBMODULE_UNSUPPORTED": policy.GitObjectResult.UNSUPPORTED_OBJECT,
}


def _run(
    *arguments: str,
    cwd: Path,
    check: bool = True,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(arguments),
        cwd=cwd,
        check=check,
        capture_output=True,
        input=input_bytes,
    )


def _git(*arguments: str, cwd: Path) -> str:
    return _run("git", *arguments, cwd=cwd).stdout.decode("ascii").strip()


def _init_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "secret_check.sh",
        "secret_scanner.py",
        "git_object_secret_policy.py",
    ):
        shutil.copy2(SOURCE_ROOT / "scripts" / name, scripts / name)
    (scripts / "__init__.py").write_text("", encoding="utf-8")
    (repository / "baseline.txt").write_text("clean baseline\n", encoding="utf-8")
    _git("init", "--quiet", cwd=repository)
    _git("config", "user.name", "Synthetic Test", cwd=repository)
    _git("config", "user.email", "synthetic@example.invalid", cwd=repository)
    _git("add", ".", cwd=repository)
    _git("commit", "--quiet", "-m", "synthetic baseline", cwd=repository)
    base_sha = _git("rev-parse", "HEAD", cwd=repository)
    base_tree = _git("rev-parse", f"{base_sha}^{{tree}}", cwd=repository)
    return repository, base_sha, base_tree


def _stage_case(repository: Path, case: CallerCase) -> None:
    if case.kind in {"regular", "executable"}:
        (repository / "candidate").write_bytes(case.content)
        _git("add", "candidate", cwd=repository)
        if case.kind == "executable":
            _git("update-index", "--chmod=+x", "candidate", cwd=repository)
        return
    if case.kind == "symlink":
        oid = (
            _run(
                "git",
                "hash-object",
                "-w",
                "--stdin",
                cwd=repository,
                input_bytes=case.content,
            )
            .stdout.decode("ascii")
            .strip()
        )
        _git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{oid},candidate",
            cwd=repository,
        )
        return
    if case.kind == "gitlink":
        oid = _git("rev-parse", "HEAD", cwd=repository)
        _git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{oid},candidate",
            cwd=repository,
        )
        return
    raise AssertionError(case.kind)


def _repository_result(
    repository: Path,
) -> tuple[
    policy.GitObjectResult,
    str,
    int,
    subprocess.CompletedProcess[bytes],
]:
    completed = _run(
        "bash",
        "scripts/secret_check.sh",
        cwd=repository,
        check=False,
    )
    output = (completed.stdout + completed.stderr).decode("utf-8")
    if completed.returncode == 0:
        return policy.GitObjectResult.CLEAN, "CLEAN", 0, completed
    result_match = re.search(r"\bresult=([A-Z_]+)\b", output)
    exit_class_match = re.search(r"\bclass=([A-Z_]+)\b", output)
    assert result_match is not None
    assert exit_class_match is not None
    return (
        policy.GitObjectResult(result_match.group(1)),
        exit_class_match.group(1),
        completed.returncode,
        completed,
    )


def _context_result(
    *,
    repository: Path,
    base_sha: str,
    base_tree: str,
    operation_root: Path,
) -> tuple[policy.GitObjectResult, str, int]:
    _git("commit", "--quiet", "-m", "synthetic candidate", cwd=repository)
    source_sha = _git("rev-parse", "HEAD", cwd=repository)
    source_tree = _git("rev-parse", f"{source_sha}^{{tree}}", cwd=repository)
    operation_root.mkdir()
    try:
        build_helper.export_exact_git_context(
            repository_root=repository,
            source_sha=source_sha,
            source_tree_sha=source_tree,
            approved_base_sha=base_sha,
            approved_base_tree_sha=base_tree,
            operation_root=operation_root,
        )
    except build_helper.BuildContractError as exc:
        return _CONTEXT_RESULT_BY_CODE[exc.code], exc.exit_class, exc.exit_code
    return policy.GitObjectResult.CLEAN, "CLEAN", 0


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
def test_real_callers_apply_identical_git_object_policy(
    tmp_path: Path,
    case: CallerCase,
) -> None:
    repository, base_sha, base_tree = _init_repository(tmp_path)
    _stage_case(repository, case)

    (
        repository_result,
        repository_exit_class,
        repository_exit_code,
        completed,
    ) = _repository_result(repository)
    context_result, context_exit_class, context_exit_code = _context_result(
        repository=repository,
        base_sha=base_sha,
        base_tree=base_tree,
        operation_root=tmp_path / "operation",
    )
    output = completed.stdout + completed.stderr

    assert repository_result is case.expected
    assert context_result is case.expected
    assert repository_result is context_result
    assert repository_exit_class == context_exit_class
    assert repository_exit_code == context_exit_code
    assert (repository_exit_code == 0) is (
        case.expected is policy.GitObjectResult.CLEAN
    )
    if case.content:
        assert case.content not in output
    assert b"source line" not in output.lower()


@pytest.mark.parametrize(
    ("kind", "mode"),
    [("symlink", "120000"), ("gitlink", "160000")],
)
def test_real_callers_deny_staged_type_changes_identically(
    tmp_path: Path,
    kind: str,
    mode: str,
) -> None:
    repository, _, _ = _init_repository(tmp_path)
    candidate = repository / "candidate"
    candidate.write_text("regular baseline\n", encoding="utf-8")
    _git("add", "candidate", cwd=repository)
    _git("commit", "--quiet", "-m", "regular candidate", cwd=repository)
    base_sha = _git("rev-parse", "HEAD", cwd=repository)
    base_tree = _git("rev-parse", f"{base_sha}^{{tree}}", cwd=repository)

    if kind == "symlink":
        oid = (
            _run(
                "git",
                "hash-object",
                "-w",
                "--stdin",
                cwd=repository,
                input_bytes=b"synthetic-target",
            )
            .stdout.decode("ascii")
            .strip()
        )
    else:
        oid = base_sha
    _git(
        "update-index",
        "--cacheinfo",
        f"{mode},{oid},candidate",
        cwd=repository,
    )

    assert _git("diff", "--cached", "--name-status", cwd=repository) == (
        "T\tcandidate"
    )
    (
        repository_result,
        repository_exit_class,
        repository_exit_code,
        completed,
    ) = _repository_result(repository)
    context_result, context_exit_class, context_exit_code = _context_result(
        repository=repository,
        base_sha=base_sha,
        base_tree=base_tree,
        operation_root=tmp_path / "operation",
    )

    assert repository_result is policy.GitObjectResult.UNSUPPORTED_OBJECT
    assert context_result is repository_result
    assert repository_exit_class == context_exit_class == "SECURITY_DENIED"
    assert repository_exit_code == context_exit_code == 1
    assert b"synthetic-target" not in completed.stdout + completed.stderr


def test_unstaged_worktree_bytes_do_not_replace_index_blob(
    tmp_path: Path,
) -> None:
    repository, _, _ = _init_repository(tmp_path)
    candidate = repository / "candidate"
    candidate.write_text("clean staged value\n", encoding="utf-8")
    _git("add", "candidate", cwd=repository)
    candidate.write_bytes(b"\x00" + synthetic_assignment().encode())

    result, exit_class, exit_code, completed = _repository_result(repository)

    assert result is policy.GitObjectResult.CLEAN
    assert exit_class == "CLEAN"
    assert exit_code == 0
    assert completed.returncode == 0
    assert synthetic_assignment().encode() not in completed.stdout + completed.stderr


@pytest.mark.parametrize(
    ("mode", "object_type", "expected"),
    [
        ("100664", "blob", policy.GitObjectResult.UNSUPPORTED_OBJECT),
        ("120000", "blob", policy.GitObjectResult.UNSUPPORTED_OBJECT),
        ("160000", "commit", policy.GitObjectResult.UNSUPPORTED_OBJECT),
    ],
)
def test_unsupported_modes_are_denied_before_blob_read(
    mode: str,
    object_type: str,
    expected: policy.GitObjectResult,
) -> None:
    reads = 0

    def deny_read(_oid: str) -> bytes:
        nonlocal reads
        reads += 1
        raise AssertionError("unsupported object content must not be read")

    descriptor = policy.GitObjectDescriptor(
        path="candidate",
        mode=mode,
        object_type=object_type,
        oid="0" * 40,
    )

    repository_outcome = policy.scan_descriptors(
        repository_root=SOURCE_ROOT,
        descriptors=(descriptor,),
        reader=deny_read,
    )[0]
    context_outcome = build_helper.scan_descriptors(
        repository_root=SOURCE_ROOT,
        descriptors=(descriptor,),
        reader=deny_read,
    )[0]

    assert repository_outcome.result is expected
    assert context_outcome.result is expected
    assert repository_outcome.exit_class == context_outcome.exit_class
    assert repository_outcome.exit_code == context_outcome.exit_code
    assert reads == 0


def test_missing_git_object_is_denied_identically(tmp_path: Path) -> None:
    repository, _, _ = _init_repository(tmp_path)
    descriptor = policy.GitObjectDescriptor(
        path="candidate",
        mode="100644",
        object_type="blob",
        oid="f" * 40,
    )

    repository_outcome = policy.scan_descriptors(
        repository_root=repository,
        descriptors=(descriptor,),
    )[0]
    context_outcome = build_helper.scan_descriptors(
        repository_root=repository,
        descriptors=(descriptor,),
    )[0]

    assert repository_outcome.result is policy.GitObjectResult.READ_ERROR
    assert context_outcome.result is repository_outcome.result
    assert context_outcome.exit_class == repository_outcome.exit_class
    assert context_outcome.exit_code == repository_outcome.exit_code == 2


def test_git_object_read_failure_is_denied_identically() -> None:
    descriptor = policy.GitObjectDescriptor(
        path="candidate",
        mode="100644",
        object_type="blob",
        oid="0" * 40,
    )

    def fail_read(_oid: str) -> bytes:
        raise policy.GitObjectReadError

    repository_outcome = policy.scan_descriptors(
        repository_root=SOURCE_ROOT,
        descriptors=(descriptor,),
        reader=fail_read,
    )[0]
    context_outcome = build_helper.scan_descriptors(
        repository_root=SOURCE_ROOT,
        descriptors=(descriptor,),
        reader=fail_read,
    )[0]

    assert repository_outcome.result is policy.GitObjectResult.READ_ERROR
    assert context_outcome.result is repository_outcome.result
    assert context_outcome.exit_class == repository_outcome.exit_class
    assert context_outcome.exit_code == repository_outcome.exit_code == 2


def test_scanner_internal_failure_is_denied_identically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = policy.GitObjectDescriptor(
        path="candidate",
        mode="100644",
        object_type="blob",
        oid="0" * 40,
    )

    def fail_scan(_data: bytes) -> tuple[object, ...]:
        raise RuntimeError("synthetic internal failure")

    monkeypatch.setattr(policy, "scan_secret_bytes", fail_scan)
    repository_outcome = policy.scan_descriptors(
        repository_root=SOURCE_ROOT,
        descriptors=(descriptor,),
        reader=lambda _oid: b"clean",
    )[0]
    context_outcome = build_helper.scan_descriptors(
        repository_root=SOURCE_ROOT,
        descriptors=(descriptor,),
        reader=lambda _oid: b"clean",
    )[0]

    assert repository_outcome.result is policy.GitObjectResult.INTERNAL_ERROR
    assert context_outcome.result is repository_outcome.result
    assert context_outcome.exit_class == repository_outcome.exit_class
    assert context_outcome.exit_code == repository_outcome.exit_code == 2


def test_oversized_blob_is_denied_without_partial_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = policy.GitObjectDescriptor(
        path="candidate",
        mode="100644",
        object_type="blob",
        oid="0" * 40,
    )
    monkeypatch.setattr(policy, "MAX_GIT_BLOB_BYTES", 4)

    outcome = policy.scan_git_object(descriptor, lambda _oid: b"12345")

    assert outcome.result is policy.GitObjectResult.UNSUPPORTED_OBJECT
    assert outcome.error_code == "GIT_BLOB_SIZE_UNSUPPORTED"
    assert outcome.size == 5


def test_base_binary_oid_is_provenance_bound_but_new_binary_is_denied(
    tmp_path: Path,
) -> None:
    repository, _, _ = _init_repository(tmp_path)
    (repository / "approved-binary").write_bytes(b"\x00approved historical asset")
    _git("add", "approved-binary", cwd=repository)
    _git("commit", "--quiet", "-m", "approved binary baseline", cwd=repository)
    base_sha = _git("rev-parse", "HEAD", cwd=repository)
    base_tree = _git("rev-parse", f"{base_sha}^{{tree}}", cwd=repository)
    (repository / "candidate").write_text("clean candidate\n", encoding="utf-8")
    _git("add", "candidate", cwd=repository)

    clean_result, clean_exit_class, clean_exit_code, _ = _repository_result(repository)
    context_result, context_exit_class, context_exit_code = _context_result(
        repository=repository,
        base_sha=base_sha,
        base_tree=base_tree,
        operation_root=tmp_path / "clean-operation",
    )

    assert clean_result is policy.GitObjectResult.CLEAN
    assert context_result is policy.GitObjectResult.CLEAN
    assert clean_exit_class == context_exit_class == "CLEAN"
    assert clean_exit_code == context_exit_code == 0

    (repository / "new-binary").write_bytes(b"\x00new candidate")
    _git("add", "new-binary", cwd=repository)
    denied_result, denied_exit_class, denied_exit_code, _ = _repository_result(
        repository
    )

    assert denied_result is policy.GitObjectResult.BINARY_DENIED
    assert denied_exit_class == "SECURITY_DENIED"
    assert denied_exit_code == 1


def test_policy_has_no_lossy_decode_or_path_allowlist() -> None:
    source = Path(policy.__file__).read_text(encoding="utf-8")

    assert 'errors="ignore"' not in source
    assert 'errors="replace"' not in source
    assert "suffix" not in source.casefold()
    assert "follow_symlinks" not in source


def test_direct_export_revalidates_bound_base_tree_identity(
    tmp_path: Path,
) -> None:
    repository, base_sha, _ = _init_repository(tmp_path)
    (repository / "candidate").write_text("clean candidate\n", encoding="utf-8")
    _git("add", "candidate", cwd=repository)
    _git("commit", "--quiet", "-m", "synthetic candidate", cwd=repository)
    source_sha = _git("rev-parse", "HEAD", cwd=repository)
    source_tree = _git("rev-parse", f"{source_sha}^{{tree}}", cwd=repository)

    with pytest.raises(
        build_helper.BuildContractError,
        match="^GIT_EXPORT_IDENTITY_MISMATCH$",
    ):
        build_helper.export_exact_git_context(
            repository_root=repository,
            source_sha=source_sha,
            source_tree_sha=source_tree,
            approved_base_sha=base_sha,
            approved_base_tree_sha="0" * 40,
            operation_root=tmp_path / "operation",
        )

    assert not (tmp_path / "operation" / "git-tree-context").exists()


def test_exit_code_aggregation_prioritizes_internal_error() -> None:
    descriptor = policy.GitObjectDescriptor(
        path="candidate",
        mode="100644",
        object_type="blob",
        oid="0" * 40,
    )
    security = policy.GitObjectScanOutcome(
        descriptor=descriptor,
        result=policy.GitObjectResult.BINARY_DENIED,
        error_code="GIT_BINARY_BLOB_DENIED",
        size=1,
    )
    internal = policy.GitObjectScanOutcome(
        descriptor=descriptor,
        result=policy.GitObjectResult.INTERNAL_ERROR,
        error_code="GIT_OBJECT_SCAN_INTERNAL_ERROR",
        size=None,
    )

    assert policy.aggregate_exit_code(()) == 0
    assert policy.aggregate_exit_code((security,)) == 1
    assert policy.aggregate_exit_code((security, internal)) == 2
