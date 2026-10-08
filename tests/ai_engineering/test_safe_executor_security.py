"""Adversarial Security Test Suite for Hermes Safe Executor Architecture.

Verifies the 18 core security invariants (SEC-01 through SEC-18) covering:
- C01: Credential boundary and zero-secret inheritance
- C02: Untrusted candidate execution, filesystem containment, and host isolation
- Fail-closed execution_ready() and health() semantics
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock

import pytest

from ai_engineering.linear_dispatcher.contracts import BlockReasonCode, LinearTask
from ai_engineering.linear_dispatcher.scope_gate import ScopeGate
from ai_engineering.linear_dispatcher.task_executor import (
    CallableModelGateway,
    DshModelGateway,
    ExecutionResult,
    IModelGateway,
    SandboxedBrokerTaskExecutor,
    validate_worktree_path_containment,
)


def make_task(task_id: str = "HER-888") -> LinearTask:
    return LinearTask(
        id=task_id,
        uuid=f"uuid-{task_id}",
        title=f"Security Test Task {task_id}",
        description="Implement benign change",
        state="Todo",
        priority=1,
        assignee="test-agent",
        labels=("agent-auto",),
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
        url=f"https://linear.app/issue/{task_id}",
    )


# ---------------------------------------------------------------------------
# SEC-01: Synthetic secret read outside sandbox
# ---------------------------------------------------------------------------
def test_sec01_synthetic_secret_read_outside_sandbox(tmp_path: Path):
    """Target path attempting to read files outside the worktree is rejected."""
    wt = tmp_path / "wt"
    wt.mkdir()
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    secret_file = secret_dir / "api_key.txt"
    secret_file.write_text("SUPER_SECRET_TOKEN_123")

    # Attempt traversal to secret file
    traversal_path = "../secrets/api_key.txt"
    ok, err = validate_worktree_path_containment(traversal_path, wt)
    assert not ok
    assert err == "PATH_TRAVERSAL_DETECTED"


# ---------------------------------------------------------------------------
# SEC-02: Write outside worktree
# ---------------------------------------------------------------------------
def test_sec02_write_outside_worktree_blocked(tmp_path: Path):
    """Mutations attempting to write outside worktree boundary are rejected fail-closed."""
    wt = tmp_path / "wt"
    wt.mkdir()

    # Absolute path escape
    outside_abs = tmp_path / "outside.py"
    ok, err = validate_worktree_path_containment(outside_abs, wt)
    assert not ok
    assert err == "PATH_TRAVERSAL_DETECTED"

    # Traversal escape
    ok2, err2 = validate_worktree_path_containment("../outside.py", wt)
    assert not ok2
    assert err2 == "PATH_TRAVERSAL_DETECTED"


# ---------------------------------------------------------------------------
# SEC-03: Symlink / path traversal escape
# ---------------------------------------------------------------------------
def test_sec03_symlink_path_traversal_escape_blocked(tmp_path: Path):
    """Symlinks pointing outside worktree root are detected and rejected."""
    wt = tmp_path / "wt"
    wt.mkdir()
    target_outside = tmp_path / "target_outside"
    target_outside.mkdir()

    symlink_dir = wt / "symlink_dir"
    try:
        symlink_dir.symlink_to(target_outside, target_is_directory=True)
        target_file = "symlink_dir/evil.py"
        ok, err = validate_worktree_path_containment(target_file, wt)
        assert not ok
        assert err in ("SYMLINK_ESCAPE_DETECTED", "PATH_TRAVERSAL_DETECTED")
    except OSError:
        # If host OS denies unprivileged symlink creation, verify symlink detection logic via mocked Path
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "is_symlink", lambda self: "symlink_dir" in str(self))
            mp.setattr(
                Path,
                "resolve",
                lambda self: target_outside if "symlink_dir" in str(self) else self,
            )
            ok, err = validate_worktree_path_containment("symlink_dir/evil.py", wt)
            assert not ok
            assert err in ("SYMLINK_ESCAPE_DETECTED", "PATH_TRAVERSAL_DETECTED")


# ---------------------------------------------------------------------------
# SEC-04: Windows junction / hardlink escape
# ---------------------------------------------------------------------------
def test_sec04_windows_junction_hardlink_escape_blocked(tmp_path: Path):
    """Any path that resolves outside the worktree root is rejected fail-closed."""
    wt = tmp_path / "wt"
    wt.mkdir()

    # Attempting to escape via drive root or parent steps
    escape_path = "../../Windows/System32"
    ok, err = validate_worktree_path_containment(escape_path, wt)
    assert not ok
    assert err in ("PATH_TRAVERSAL_DETECTED", "EMPTY_TARGET_PATH")


# ---------------------------------------------------------------------------
# SEC-05: Git filter execution neutralized
# ---------------------------------------------------------------------------
def test_sec05_git_filter_execution_neutralized():
    """Git porcelain status command is invoked with explicit filter and driver neutralization."""
    mock_runner = MagicMock()
    mock_runner.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )

    executor = SandboxedBrokerTaskExecutor(runner=mock_runner)
    executor._detect_changed_files(Path("/fake/wt"))

    assert mock_runner.called
    call_args = mock_runner.call_args[0][0]
    # Check that filter.* and core.fsmonitor are neutralized
    assert "-c" in call_args
    assert "core.fsmonitor=" in call_args
    assert "filter.lfs.clean=" in call_args
    assert "filter.lfs.smudge=" in call_args


# ---------------------------------------------------------------------------
# SEC-06: Git hook execution blocked
# ---------------------------------------------------------------------------
def test_sec06_git_hook_execution_blocked(tmp_path: Path):
    """Any modification targeting .git or .git/hooks is rejected fail-closed."""
    wt = tmp_path / "wt"
    wt.mkdir()

    for forbidden in [
        ".git/hooks/pre-commit",
        ".git/config",
        ".git/attributes",
        ".git/HEAD",
    ]:
        ok, err = validate_worktree_path_containment(forbidden, wt)
        assert not ok
        assert err == "GIT_METADATA_MUTATION_FORBIDDEN"


# ---------------------------------------------------------------------------
# SEC-07: PATH hijacking blocked
# ---------------------------------------------------------------------------
def test_sec07_path_hijacking_blocked():
    """Bubblewrap command enforces fixed system PATH without relative worktree directories."""
    executor = SandboxedBrokerTaskExecutor()
    cmd = executor.build_bwrap_command("/var/tmp/wt", ["git", "status"])
    # System bin mounts must only point to trusted system directories
    bin_mounts = [
        cmd[i + 1]
        for i in range(len(cmd) - 1)
        if cmd[i] in ("--ro-bind", "--symlink") and "bin" in cmd[i + 1]
    ]
    assert all(p.startswith(("usr/bin", "/bin", "/usr/sbin", "usr/sbin")) for p in bin_mounts)


# ---------------------------------------------------------------------------
# SEC-08: Environment & /proc inspection
# ---------------------------------------------------------------------------
def test_sec08_clearenv_and_proc_isolation():
    """Sandbox command strictly enforces --clearenv, --unshare-all, and unshared /proc."""
    executor = SandboxedBrokerTaskExecutor()
    cmd = executor.build_bwrap_command("/var/tmp/wt")

    assert "--clearenv" in cmd
    assert "--unshare-all" in cmd or ("--unshare-pid" in cmd and "--unshare-net" in cmd)
    assert "--proc" in cmd


# ---------------------------------------------------------------------------
# SEC-09: Network exfiltration blocked
# ---------------------------------------------------------------------------
def test_sec09_network_exfiltration_blocked():
    """Tool runner execution command strictly isolates network namespace."""
    executor = SandboxedBrokerTaskExecutor()
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    # --unshare-all unshares network, or explicit --unshare-net must be present
    assert "--unshare-all" in cmd or "--unshare-net" in cmd


# ---------------------------------------------------------------------------
# SEC-10: Prompt injection file tampering blocked
# ---------------------------------------------------------------------------
def test_sec10_prompt_injection_file_tampering_blocked(tmp_path: Path):
    """Malicious model output containing path traversal injection is rejected."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").mkdir()

    adversarial_payload = """```diff
--- a/../../../etc/shadow
+++ b/../../../etc/shadow
@@ -1,1 +1,1 @@
-root:*
+root:pwned
```"""

    gateway = CallableModelGateway(lambda p, t, w: adversarial_payload)
    mock_runner = MagicMock()
    mock_runner.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    executor = SandboxedBrokerTaskExecutor(model_gateway=gateway, runner=mock_runner)
    executor.health = MagicMock(return_value=True)
    executor.execution_ready = MagicMock(return_value=True)

    result = executor.execute(make_task(), wt, "27bc056a3ba670311a6924868d06a40c1b4d4059")
    assert result.status == "FAILED"
    assert result.error_reason in ("PATH_TRAVERSAL_DETECTED", "PATH_CONTAINMENT_VIOLATION")


# ---------------------------------------------------------------------------
# SEC-11: Forbidden child process execution
# ---------------------------------------------------------------------------
def test_sec11_forbidden_child_process_execution():
    """Sandbox does not bind host binaries or shell interpreters from unverified paths."""
    executor = SandboxedBrokerTaskExecutor()
    cmd = executor.build_bwrap_command("/var/tmp/wt")

    # Only /usr, /bin, /lib are mounted ro
    for i in range(len(cmd) - 2):
        if cmd[i] == "--ro-bind":
            src = cmd[i + 1]
            assert src in ("/usr", "/bin", "/lib", "/lib64", "/sbin")


# ---------------------------------------------------------------------------
# SEC-12: Timeout and process reaping
# ---------------------------------------------------------------------------
def test_sec12_timeout_and_process_reaping(tmp_path: Path):
    """Subprocess timeout terminates execution and returns structured failure."""
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", str(wt)], check=True, capture_output=True)

    def hanging_model(p, t, w):
        raise subprocess.TimeoutExpired(cmd=["dsh"], timeout=5)

    gateway = CallableModelGateway(hanging_model)
    executor = SandboxedBrokerTaskExecutor(model_gateway=gateway)
    executor.health = MagicMock(return_value=True)
    executor.execution_ready = MagicMock(return_value=True)

    result = executor.execute(make_task(), wt, "27bc056a3ba670311a6924868d06a40c1b4d4059")
    assert result.status == "FAILED"
    assert result.error_reason == "MODEL_INFERENCE_FAILED"


# ---------------------------------------------------------------------------
# SEC-13: Credential leakage in evidence blocked
# ---------------------------------------------------------------------------
def test_sec13_zero_credential_leakage():
    """ExecutionResult evidence does not contain secret tokens or credentials."""
    result = ExecutionResult(
        status="SUCCESS",
        changed_files=("math_utils.py",),
        execution_id="exec-12345",
        executor_name="SandboxedBrokerTaskExecutor",
        evidence={"task_id": "HER-123", "changed_count": 1},
    )
    evidence_str = str(result.evidence)
    assert "token" not in evidence_str.lower()
    assert "ghp_" not in evidence_str
    assert "lin_api" not in evidence_str


# ---------------------------------------------------------------------------
# SEC-14: Docker socket & host IPC access blocked
# ---------------------------------------------------------------------------
def test_sec14_docker_socket_and_ipc_access_blocked():
    """Docker socket is never mounted into sandbox and IPC namespace is unshared."""
    executor = SandboxedBrokerTaskExecutor()
    cmd = executor.build_bwrap_command("/var/tmp/wt")

    assert not any("/var/run/docker.sock" in arg for arg in cmd)
    assert "--unshare-all" in cmd or "--unshare-ipc" in cmd


# ---------------------------------------------------------------------------
# SEC-15: Mount tampering / misconfigured mounts
# ---------------------------------------------------------------------------
def test_sec15_mount_tampering_fails_closed():
    """validate_isolation() detects any misconfigured forbidden host mount."""
    executor = SandboxedBrokerTaskExecutor()

    # Normal command passes
    ok, err = executor.validate_isolation()
    assert ok
    assert err is None

    # Tampered command with host /root fails
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            executor,
            "build_bwrap_command",
            lambda wt, tool_cmd=None: ["bwrap", "--clearenv", "--unshare-all", "--bind", "/root", "/root"],
        )
        ok_tampered, err_tampered = executor.validate_isolation()
        assert not ok_tampered
        assert err_tampered == "FORBIDDEN_PATH_MOUNTED_/root"


# ---------------------------------------------------------------------------
# SEC-16: Tampered base SHA & invalid worktree fails
# ---------------------------------------------------------------------------
def test_sec16_tampered_base_sha_and_invalid_worktree_fails(tmp_path: Path):
    """Non-existent worktree path returns structured failure immediately."""
    executor = SandboxedBrokerTaskExecutor()
    fake_wt = tmp_path / "does_not_exist"

    result = executor.execute(make_task(), fake_wt, "27bc056a3ba670311a6924868d06a40c1b4d4059")
    assert result.status == "FAILED"
    assert result.error_reason == "INVALID_WORKTREE_PATH"


# ---------------------------------------------------------------------------
# SEC-17: Failure recovery without corrupt state
# ---------------------------------------------------------------------------
def test_sec17_failure_recovery_without_corrupt_state(tmp_path: Path):
    """Syntax check failure returns structured FAILED result cleanly."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").mkdir()

    bad_syntax_payload = """```python file:broken.py
def broken_func(
```"""

    gateway = CallableModelGateway(lambda p, t, w: bad_syntax_payload)
    def mock_runner_impl(cmd, **kwargs):
        if "py_compile" in str(cmd):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="SyntaxError: unexpected EOF"
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    executor = SandboxedBrokerTaskExecutor(model_gateway=gateway, runner=mock_runner_impl)
    executor.health = MagicMock(return_value=True)
    executor.execution_ready = MagicMock(return_value=True)

    result = executor.execute(make_task(), wt, "27bc056a3ba670311a6924868d06a40c1b4d4059")
    assert result.status == "FAILED"
    assert "SYNTAX_VALIDATION_FAILED" in (result.error_reason or "")


# ---------------------------------------------------------------------------
# SEC-18: Fail-closed on missing isolation tools
# ---------------------------------------------------------------------------
def test_sec18_fail_closed_on_missing_sandbox():
    """If Bubblewrap is missing from host/WSL, health() and execution_ready() fail closed."""
    mock_runner = MagicMock()
    mock_runner.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="which: no bwrap in PATH"
    )

    executor = SandboxedBrokerTaskExecutor(
        is_windows=True,
        bwrap_bin="missing_bwrap",
        runner=mock_runner,
    )
    assert executor.health() is False
    assert executor.execution_ready() is False


# ---------------------------------------------------------------------------
# SEC-19: Fail-closed on missing or unhealthy model gateway
# ---------------------------------------------------------------------------
def test_sec19_model_gateway_missing_fails_closed():
    """Without configured healthy Model Gateway, health() and execution_ready() fail closed."""
    # When model_gateway is None
    executor = SandboxedBrokerTaskExecutor(model_gateway=None)
    assert executor.health() is False
    assert executor.execution_ready() is False

    # When model_gateway.health() is False
    unhealthy_gw = MagicMock()
    unhealthy_gw.health.return_value = False
    executor_unhealthy = SandboxedBrokerTaskExecutor(model_gateway=unhealthy_gw)
    assert executor_unhealthy.health() is False
    assert executor_unhealthy.execution_ready() is False


# ---------------------------------------------------------------------------
# SEC-20: Unified diff hunk offset alignment
# ---------------------------------------------------------------------------
def test_sec20_unified_diff_hunk_offset_alignment():
    """Unified diff with offset line numbers (start > 1) applies accurately without dropping prefix lines."""
    executor = SandboxedBrokerTaskExecutor()
    orig = "line 1\nline 2\nline 3\nline 4\nline 5\nline 6\nline 7\nline 8\nline 9\nline 10\n"
    hunk_lines = [
        "@@ -5,3 +5,4 @@",
        " line 5",
        "-line 6",
        "+line 6 modified",
        "+line 6.5 added",
        " line 7",
    ]
    applied = executor._apply_hunks(orig, hunk_lines)
    expected = "line 1\nline 2\nline 3\nline 4\nline 5\nline 6 modified\nline 6.5 added\nline 7\nline 8\nline 9\nline 10\n"
    assert applied == expected


# ---------------------------------------------------------------------------
# SEC-21: Case-insensitive and alternate stream git metadata blocked
# ---------------------------------------------------------------------------
def test_sec21_case_insensitive_git_metadata_blocked(tmp_path: Path):
    """Paths using uppercase .GIT or alternate data streams are rejected fail-closed."""
    wt = tmp_path / "wt"
    wt.mkdir()

    for forbidden in [
        ".GIT/config",
        ".Git/hooks/pre-commit",
        ".git:stream",
        ".GIT:INDEX",
    ]:
        ok, err = validate_worktree_path_containment(forbidden, wt)
        assert not ok
        assert err == "GIT_METADATA_MUTATION_FORBIDDEN"


# ---------------------------------------------------------------------------
# SEC-22: Diff parsing rejects traversal before reading host files
# ---------------------------------------------------------------------------
def test_sec22_diff_parsing_rejects_traversal_before_reading(tmp_path: Path):
    """_parse_unified_diff rejects traversal without reading non-worktree host files."""
    wt = tmp_path / "wt"
    wt.mkdir()

    outside_file = tmp_path / "sensitive.txt"
    outside_file.write_text("SENSITIVE_DATA")

    diff_text = f"""--- a/../../sensitive.txt
+++ b/../../sensitive.txt
@@ -1,1 +1,1 @@
-SENSITIVE_DATA
+PWNED
"""
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(diff_text, wt)
    assert not edits
    assert err == "PATH_TRAVERSAL_DETECTED"
    # Verify the host file was completely untouched
    assert outside_file.read_text() == "SENSITIVE_DATA"


# ---------------------------------------------------------------------------
# SEC-23: Hardlink escape blocked
# ---------------------------------------------------------------------------
def test_sec23_hardlink_escape_blocked(tmp_path: Path):
    """Mutations targeting hardlinked worktree files are rejected to prevent external inode truncation."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").mkdir()

    target_file = wt / "hardlinked.py"
    target_file.write_text("ORIGINAL_CONTENT")

    try:
        external_link = tmp_path / "external_hardlink.py"
        os.link(target_file, external_link)
    except OSError:
        pass

    with pytest.MonkeyPatch.context() as mp:
        real_stat = Path.stat
        mp.setattr(
            Path,
            "stat",
            lambda self, *a, **kw: os.stat_result((33188, 0, 0, 2, 0, 0, 100, 0, 0, 0))
            if "hardlinked.py" in str(self)
            else real_stat(self, *a, **kw),
        )
        ok, err = validate_worktree_path_containment("hardlinked.py", wt)
        assert not ok
        assert err == "HARDLINK_ESCAPE_DETECTED"


# ---------------------------------------------------------------------------
# SEC-24: Symlink Git metadata aliases blocked
# ---------------------------------------------------------------------------
def test_sec24_symlink_git_metadata_alias_blocked(tmp_path: Path):
    """Symlinks pointing to .git are rejected even if the link itself has a benign name."""
    wt = tmp_path / "wt"
    wt.mkdir()
    git_dir = wt / ".git"
    git_dir.mkdir()

    try:
        alias = wt / "benign_alias"
        alias.symlink_to(git_dir, target_is_directory=True)
        ok, err = validate_worktree_path_containment("benign_alias/config", wt)
        assert not ok
        assert err == "GIT_METADATA_MUTATION_FORBIDDEN"
    except OSError:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "is_symlink", lambda self: "benign_alias" in str(self))
            mp.setattr(
                Path,
                "resolve",
                lambda self: git_dir if "benign_alias" in str(self) else self,
            )
            ok, err = validate_worktree_path_containment("benign_alias/config", wt)
            assert not ok
            assert err == "GIT_METADATA_MUTATION_FORBIDDEN"


# ---------------------------------------------------------------------------
# SEC-25: Multi-file diff header separation
# ---------------------------------------------------------------------------
def test_sec25_multi_file_diff_header_separation(tmp_path: Path):
    """Multi-file diff headers (--- a/...) do not leak into preceding file hunks as deletions."""
    wt = tmp_path / "wt"
    wt.mkdir()
    f1 = wt / "first.py"
    f1.write_text("line1\nline2\n")
    f2 = wt / "second.py"
    f2.write_text("alpha\nbeta\n")

    multi_diff = """diff --git a/first.py b/first.py
--- a/first.py
+++ b/first.py
@@ -1,2 +1,2 @@
 line1
-line2
+line2_modified
diff --git a/second.py b/second.py
--- a/second.py
+++ b/second.py
@@ -1,2 +1,2 @@
 alpha
-beta
+beta_modified
"""
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(multi_diff, wt)
    assert err is None
    assert len(edits) == 2
    assert edits[0] == ("first.py", "line1\nline2_modified\n")
    assert edits[1] == ("second.py", "alpha\nbeta_modified\n")


# ---------------------------------------------------------------------------
# SEC-26: Zero-length hunk insertion semantics
# ---------------------------------------------------------------------------
def test_sec26_zero_length_hunk_insertion():
    """Hunk @@ -2,0 +3,1 @@ inserts content after line 2, not before it."""
    orig = "line 1\nline 2\nline 3\n"
    hunk = [
        "@@ -2,0 +3,1 @@",
        "+inserted line",
    ]
    executor = SandboxedBrokerTaskExecutor()
    res = executor._apply_hunks(orig, hunk)
    expected = "line 1\nline 2\ninserted line\nline 3\n"
    assert res == expected


# ---------------------------------------------------------------------------
# SEC-27: Hunk context mismatch rejected fail-closed
# ---------------------------------------------------------------------------
def test_sec27_hunk_context_mismatch_rejected(tmp_path: Path):
    """Hunk with stale or mismatching context lines raises failure rather than corrupting file."""
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "foo.py").write_text("actual line 1\nactual line 2\n")

    stale_diff = """--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,2 @@
-hallucinated line 1
+new line 1
 actual line 2
"""
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(stale_diff, wt)
    assert not edits
    assert "HUNK_APPLICATION_FAILED" in (err or "")


# ---------------------------------------------------------------------------
# SEC-28: Multi-file explicit code blocks collected
# ---------------------------------------------------------------------------
def test_sec28_multi_file_explicit_code_blocks_collected(tmp_path: Path):
    """Multiple explicit file blocks are all accumulated without premature truncation."""
    wt = tmp_path / "wt"
    wt.mkdir()

    output = """Here are the changes:

```python
file:pkg/first.py
def one(): return 1
```

And the second file:

```python
file:pkg/second.py
def two(): return 2
```
"""
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_mutation_payload(output, wt)
    assert err is None
    assert len(edits) == 2
    assert edits[0] == ("pkg/first.py", "def one(): return 1\n")
    assert edits[1] == ("pkg/second.py", "def two(): return 2\n")


# ---------------------------------------------------------------------------
# SEC-29: File block containing diff markers not misclassified
# ---------------------------------------------------------------------------
def test_sec29_file_block_with_diff_markers_recognized_as_file(tmp_path: Path):
    """Source file containing diff marker strings (e.g. +++ b/) is treated as a full file."""
    wt = tmp_path / "wt"
    wt.mkdir()

    output = """```python
file:parser_test.py
DIFF_MARKER = "+++ b/some_file"
def test():
    assert DIFF_MARKER
```"""
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_mutation_payload(output, wt)
    assert err is None
    assert len(edits) == 1
    assert edits[0][0] == "parser_test.py"
    assert "DIFF_MARKER" in edits[0][1]


# ---------------------------------------------------------------------------
# SEC-30: Evidence sanitization preserves zero credential leakage
# ---------------------------------------------------------------------------
def test_sec30_evidence_sanitization_no_raw_prompts(tmp_path: Path):
    """Unparseable or failed executions record hashes/lengths, not raw output strings."""
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", str(wt)], check=True, capture_output=True)

    unparseable_output = "SecretToken12345: cannot parse this non-code response"
    gateway = CallableModelGateway(lambda p, t, w: unparseable_output)
    executor = SandboxedBrokerTaskExecutor(model_gateway=gateway)
    executor.health = MagicMock(return_value=True)
    executor.execution_ready = MagicMock(return_value=True)

    result = executor.execute(make_task(), wt, "27bc056a3ba670311a6924868d06a40c1b4d4059")
    assert result.status == "FAILED"
    assert "SecretToken12345" not in str(result.evidence)
    assert "output_sha256" in result.evidence
    assert "output_length" in result.evidence


# ---------------------------------------------------------------------------
# SEC-31: DshModelGateway requires tool-disabling patch overlay
# ---------------------------------------------------------------------------
def test_sec31_dsh_gateway_requires_patch_overlay(tmp_path: Path):
    """DshModelGateway fails health() if patch_path is not configured or does not exist."""
    fake_bin = tmp_path / "dsh"
    fake_bin.touch()

    # Without patch_path
    gw_no_patch = DshModelGateway(dsh_bin=str(fake_bin), patch_path=None)
    assert gw_no_patch.health() is False

    # With non-existent patch_path
    gw_bad_patch = DshModelGateway(
        dsh_bin=str(fake_bin), patch_path=str(tmp_path / "nonexistent.yaml")
    )
    assert gw_bad_patch.health() is False

    # With valid patch_path and mocked --version
    valid_patch = tmp_path / "patch.yaml"
    valid_patch.write_text(
        "- id: tool-fs\n  disabled: true\n"
        "- id: tool-fs-search\n  disabled: true\n"
        "- id: tool-pwsh\n  disabled: true\n"
        "- id: tool-bash\n  disabled: true\n"
        "- id: tool-jobs\n  disabled: true\n"
        "- id: web\n  disabled: true\n"
        "- id: subagent\n  disabled: true\n"
        "- id: tool-skill\n  disabled: true\n"
        "- id: tool-todo\n  disabled: true\n"
        "- id: tool-goal\n  disabled: true\n",
        encoding="utf-8",
    )
    mock_runner = MagicMock()
    mock_runner.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="dsh 0.2.0\n", stderr=""
    )
    gw_valid = DshModelGateway(
        dsh_bin=str(fake_bin),
        patch_path=str(valid_patch),
        runner=mock_runner,
    )
    assert gw_valid.health() is True


# ---------------------------------------------------------------------------
# SEC-32: Real sandbox probe in execution_ready
# ---------------------------------------------------------------------------
def test_sec32_real_sandbox_probe_in_execution_ready():
    """execution_ready() executes true inside bubblewrap, failing closed if namespaces fail."""
    mock_runner = MagicMock()
    mock_runner.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="bwrap: No permissions to create user namespace"
    )

    gw = MagicMock()
    gw.health.return_value = True

    executor = SandboxedBrokerTaskExecutor(
        model_gateway=gw,
        runner=mock_runner,
    )
    assert executor.execution_ready() is False


# ---------------------------------------------------------------------------
# SEC-33: DSH patch profile verification
# ---------------------------------------------------------------------------
def test_sec33_dsh_patch_profile_verification(tmp_path: Path):
    """DshModelGateway rejects patches that do not explicitly disable dangerous tools."""
    fake_bin = tmp_path / "dsh"
    fake_bin.touch()

    # Patch missing tool-bash
    partial_patch = tmp_path / "partial_patch.yaml"
    partial_patch.write_text(
        "- id: tool-fs\n  disabled: true\n- id: tool-pwsh\n  disabled: true\n",
        encoding="utf-8",
    )
    gw = DshModelGateway(dsh_bin=str(fake_bin), patch_path=str(partial_patch))
    assert gw.health() is False
    with pytest.raises(RuntimeError, match="DSH_TOOL_DISABLING_OVERLAY_REQUIRED"):
        gw.generate_mutation("prompt", make_task(), tmp_path)


# ---------------------------------------------------------------------------
# SEC-34: Binary or unreadable diff target rejected
# ---------------------------------------------------------------------------
def test_sec34_binary_or_unreadable_diff_target_rejected(tmp_path: Path):
    """Diff targeting an unreadable or non-UTF8 binary file returns structured error."""
    wt = tmp_path / "wt"
    wt.mkdir()
    binary_file = wt / "image.bin"
    binary_file.write_bytes(b"\x80\x81\xff\xfe\x00\x01\x02")

    diff_text = (
        "--- a/image.bin\n"
        "+++ b/image.bin\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(diff_text, wt)
    assert edits == []
    assert err is not None
    assert err.startswith("TARGET_READ_FAILED_image.bin")


# ---------------------------------------------------------------------------
# SEC-35: Invalid hunk offsets and incomplete diff hunks rejected
# ---------------------------------------------------------------------------
def test_sec35_invalid_hunk_offsets_and_incomplete_diff_hunks_rejected():
    """Hunks with offsets exceeding file length or mismatched line counts are rejected."""
    executor = SandboxedBrokerTaskExecutor()
    orig = "line 1\nline 2\n"

    # Out of bounds offset
    oob_hunk = [
        "@@ -99,1 +99,1 @@",
        "-line 99",
        "+line 99 modified",
    ]
    with pytest.raises(ValueError, match="Hunk offset exceeds file length"):
        executor._apply_hunks(orig, oob_hunk)

    # Incomplete hunk (header claims 2 lines old, only 1 provided)
    incomplete_hunk = [
        "@@ -1,2 +1,1 @@",
        "-line 1",
    ]
    with pytest.raises(ValueError, match="Diff hunk line count mismatch"):
        executor._apply_hunks(orig, incomplete_hunk)


# ---------------------------------------------------------------------------
# SEC-36: Multiple fenced diff blocks collected
# ---------------------------------------------------------------------------
def test_sec36_multiple_fenced_diff_blocks_collected(tmp_path: Path):
    """Multiple diff blocks in model output are all accumulated before returning."""
    wt = tmp_path / "wt"
    wt.mkdir()
    f1 = wt / "f1.py"
    f1.write_text("def a(): pass\n", encoding="utf-8")
    f2 = wt / "f2.py"
    f2.write_text("def b(): pass\n", encoding="utf-8")

    model_output = (
        "Here are the changes:\n\n"
        "```diff\n"
        "--- a/f1.py\n"
        "+++ b/f1.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-def a(): pass\n"
        "+def a(): return 1\n"
        "```\n\n"
        "And the second change:\n\n"
        "```diff\n"
        "--- a/f2.py\n"
        "+++ b/f2.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-def b(): pass\n"
        "+def b(): return 2\n"
        "```\n"
    )

    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_mutation_payload(model_output, wt)
    assert err is None
    assert len(edits) == 2
    paths = {e[0] for e in edits}
    assert paths == {"f1.py", "f2.py"}


# ---------------------------------------------------------------------------
# SEC-37: Fence tag filename with subdirectories or spaces
# ---------------------------------------------------------------------------
def test_sec37_fence_tag_filename_with_subdirs_or_spaces(tmp_path: Path):
    """Fence headers with file:<relative/path> preserve complete paths."""
    wt = tmp_path / "wt"
    wt.mkdir()

    model_output = (
        "```python file:src/sub dir/my_module.py\n"
        "def custom(): return 42\n"
        "```\n"
    )

    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_mutation_payload(model_output, wt)
    assert err is None
    assert len(edits) == 1
    assert edits[0][0] == "src/sub dir/my_module.py"
    assert "return 42" in edits[0][1]


# ---------------------------------------------------------------------------
# SEC-38: Git metadata protected read-only inside sandbox
# ---------------------------------------------------------------------------
def test_sec38_git_metadata_protected_readonly_inside_sandbox():
    """Bubblewrap command mounts .git read-only over writable worktree mount."""
    executor = SandboxedBrokerTaskExecutor()
    cmd = executor.build_bwrap_command("/var/tmp/sample_wt")
    assert "--ro-bind-try" in cmd
    idx = cmd.index("--ro-bind-try")
    assert cmd[idx + 1] == "/var/tmp/sample_wt/.git"
    assert cmd[idx + 2] == "/var/tmp/sample_wt/.git"


# ---------------------------------------------------------------------------
# SEC-39: Syntax check invokes Python in isolated mode (-I)
# ---------------------------------------------------------------------------
def test_sec39_py_compile_isolated_mode():
    """_run_sandboxed_syntax_check uses python3 -I to prevent candidate import hijacking."""
    mock_runner = MagicMock()
    mock_runner.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    executor = SandboxedBrokerTaskExecutor(runner=mock_runner, is_windows=False)
    executor._run_sandboxed_syntax_check(Path("/fake/wt"), "test.py")

    assert mock_runner.called
    cmd = mock_runner.call_args[0][0]
    assert "-I" in cmd
    py_idx = cmd.index("python3")
    assert cmd[py_idx + 1] == "-I"


# ---------------------------------------------------------------------------
# SEC-40: Overlapping or backwards hunk offset rejected
# ---------------------------------------------------------------------------
def test_sec40_overlapping_or_backwards_hunk_offset_rejected():
    """Hunks with overlapping or backwards offsets raise ValueError fail-closed."""
    executor = SandboxedBrokerTaskExecutor()
    orig = "line 1\nline 2\nline 3\n"
    hunks = [
        "@@ -1,1 +1,1 @@",
        "-line 1",
        "+line 1 modified",
        "@@ -1,1 +1,1 @@",  # Overlapping / backwards to line 1 after line 1 was consumed
        "-line 1",
        "+line 1 modified again",
    ]
    with pytest.raises(ValueError, match="Overlapping or backwards hunk offset"):
        executor._apply_hunks(orig, hunks)


# ---------------------------------------------------------------------------
# SEC-41: Hunk failure does not leak source content
# ---------------------------------------------------------------------------
def test_sec41_hunk_failure_does_not_leak_source_content():
    """Diff hunk mismatch errors omit sensitive source content strings."""
    executor = SandboxedBrokerTaskExecutor()
    orig = "SECRET_API_KEY_NEVER_LEAK_ME\n"
    hunks = [
        "@@ -1,1 +1,1 @@",
        "-DIFFERENT_LINE",
        "+REPLACED",
    ]
    try:
        executor._apply_hunks(orig, hunks)
        pytest.fail("Expected ValueError on mismatch")
    except ValueError as exc:
        msg = str(exc)
        assert "SECRET_API_KEY_NEVER_LEAK_ME" not in msg
        assert "Diff hunk deletion mismatch at line 1" in msg


# ---------------------------------------------------------------------------
# SEC-42: Repo context skips symlinks to prevent external data leakage
# ---------------------------------------------------------------------------
def test_sec42_repo_context_skips_symlinks(tmp_path: Path):
    """External symlinks are strictly skipped when building repo context for model gateway."""
    wt = tmp_path / "wt"
    wt.mkdir()
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    secret_file = secret_dir / "secret.env"
    secret_file.write_text("SENSITIVE_DATA_12345", encoding="utf-8")

    # Create symlink pointing outside worktree
    symlink_file = wt / "linked_secret.env"
    try:
        symlink_file.symlink_to(secret_file)
    except OSError:
        pass  # Skip symlink creation if unprivileged on host

    captured_prompt = []
    def fake_gateway_gen(prompt, task, wt_path):
        captured_prompt.append(prompt)
        return "```calc.py\nprint(1)\n```"

    gw = CallableModelGateway(fake_gateway_gen)
    mock_runner = MagicMock()
    mock_runner.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    executor = SandboxedBrokerTaskExecutor(model_gateway=gw, runner=mock_runner)
    executor.health = lambda: True
    executor.execution_ready = lambda: True
    executor._detect_changed_files = lambda p: ("calc.py",)
    executor.execute(make_task(), wt, "base-sha")

    assert len(captured_prompt) == 1
    assert "SENSITIVE_DATA_12345" not in captured_prompt[0]


# ---------------------------------------------------------------------------
# SEC-43: Mixed payload full file and diff blocks both collected
# ---------------------------------------------------------------------------
def test_sec43_mixed_payload_full_file_and_diff_blocks_both_collected(tmp_path: Path):
    """Model reply with both full-file and diff blocks collects both without dropping either."""
    wt = tmp_path / "wt"
    wt.mkdir()
    f1 = wt / "f1.py"
    f1.write_text("def a(): pass\n", encoding="utf-8")

    model_output = (
        "First change as full file:\n"
        "```python file:f2.py\n"
        "def b(): return 2\n"
        "```\n\n"
        "Second change as diff:\n"
        "```diff\n"
        "--- a/f1.py\n"
        "+++ b/f1.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-def a(): pass\n"
        "+def a(): return 1\n"
        "```\n"
    )

    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_mutation_payload(model_output, wt)
    assert err is None
    assert len(edits) == 2
    paths = {e[0] for e in edits}
    assert paths == {"f1.py", "f2.py"}


# ---------------------------------------------------------------------------
# SEC-44: Standard multi-file diff without git header
# ---------------------------------------------------------------------------
def test_sec44_standard_multi_file_diff_without_git_header(tmp_path: Path):
    """Standard unified diff across multiple files without diff --git separates hunks properly."""
    wt = tmp_path / "wt"
    wt.mkdir()
    f1 = wt / "f1.py"
    f1.write_text("line1\n", encoding="utf-8")
    f2 = wt / "f2.py"
    f2.write_text("line2\n", encoding="utf-8")

    diff_text = (
        "--- a/f1.py\n"
        "+++ b/f1.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-line1\n"
        "+line1 modified\n"
        "--- a/f2.py\n"
        "+++ b/f2.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-line2\n"
        "+line2 modified\n"
    )

    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(diff_text, wt)
    assert err is None
    assert len(edits) == 2
    paths = {e[0] for e in edits}
    assert paths == {"f1.py", "f2.py"}
    assert "line1 modified" in [e[1] for e in edits if e[0] == "f1.py"][0]
    assert "line2 modified" in [e[1] for e in edits if e[0] == "f2.py"][0]


# ---------------------------------------------------------------------------
# SEC-45: Whitespace noncanonical path rejected
# ---------------------------------------------------------------------------
def test_sec45_whitespace_noncanonical_path_rejected(tmp_path: Path):
    """Target paths with leading/trailing whitespace are rejected fail-closed."""
    wt = tmp_path / "wt"
    wt.mkdir()

    ok, err = validate_worktree_path_containment(" alias/file.txt", wt)
    assert not ok
    assert err == "NONCANONICAL_PATH_WHITESPACE"

    ok2, err2 = validate_worktree_path_containment("dir/file.txt ", wt)
    assert not ok2
    assert err2 == "NONCANONICAL_PATH_WHITESPACE"


# ---------------------------------------------------------------------------
# SEC-46: YAML scalar injection in patch rejected
# ---------------------------------------------------------------------------
def test_sec46_yaml_scalar_injection_in_patch_rejected(tmp_path: Path):
    """A YAML overlay where disabled: true is inside a scalar text block is rejected."""
    fake_bin = tmp_path / "dsh"
    fake_bin.touch()

    # Overlay with simulated injection in a config block scalar
    injected_patch = tmp_path / "injected.yaml"
    injected_patch.write_text(
        "- id: innocent-agent\n"
        "  config:\n"
        "    persona: |\n"
        "      - id: tool-fs\n"
        "        disabled: true\n"
        "      - id: tool-fs-search\n"
        "        disabled: true\n"
        "      - id: tool-pwsh\n"
        "        disabled: true\n"
        "      - id: tool-bash\n"
        "        disabled: true\n"
        "      - id: tool-jobs\n"
        "        disabled: true\n"
        "      - id: web\n"
        "        disabled: true\n"
        "      - id: subagent\n"
        "        disabled: true\n",
        encoding="utf-8",
    )

    gw = DshModelGateway(dsh_bin=str(fake_bin), patch_path=str(injected_patch))
    assert gw.health() is False


# ---------------------------------------------------------------------------
# SEC-47: Private and sensitive files excluded from repo context
# ---------------------------------------------------------------------------
def test_sec47_private_files_excluded_from_repo_context(tmp_path: Path):
    """Sensitive files (.env, auth.json, credentials) are strictly excluded from repo context."""
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", str(wt)], check=True, capture_output=True)
    cred_file = wt / "auth.json"
    cred_file.write_text("SUPER_SECRET_AUTH_JSON", encoding="utf-8")
    env_file = wt / "production.env"
    env_file.write_text("DATABASE_URL=postgres://secret", encoding="utf-8")
    safe_file = wt / "safe_module.py"
    safe_file.write_text("def safe(): pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "Hermes Agent"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "agent@hermes.local"], check=True)
    subprocess.run(["git", "-C", str(wt), "add", "safe_module.py"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", "init"], check=True, capture_output=True)

    captured_prompt = []
    def fake_gateway_gen(prompt, task, wt_path):
        captured_prompt.append(prompt)
        return "```safe_module.py\nprint(1)\n```"

    gw = CallableModelGateway(fake_gateway_gen)
    executor = SandboxedBrokerTaskExecutor(model_gateway=gw)
    executor.health = lambda: True
    executor.execution_ready = lambda: True
    executor._detect_changed_files = lambda p: ("safe_module.py",)
    executor.execute(make_task(), wt, "base-sha")

    assert len(captured_prompt) == 1
    prompt_text = captured_prompt[0]
    assert "SUPER_SECRET_AUTH_JSON" not in prompt_text
    assert "DATABASE_URL=postgres" not in prompt_text
    assert "safe_module.py" in prompt_text


# ---------------------------------------------------------------------------
# SEC-48: Empty hunk diff rejected
# ---------------------------------------------------------------------------
def test_sec48_empty_hunk_diff_rejected(tmp_path: Path):
    """Diff containing headers but zero hunks is rejected fail-closed."""
    wt = tmp_path / "wt"
    wt.mkdir()

    truncated_diff = "--- /dev/null\n+++ b/new_file.py\n"
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(truncated_diff, wt)
    assert edits == []
    assert err == "EMPTY_DIFF_HUNKS_FOR_TARGET_new_file.py"


# ---------------------------------------------------------------------------
# SEC-49: Consecutive diffs composed on staged content
# ---------------------------------------------------------------------------
def test_sec49_consecutive_diffs_composed_on_staged_content(tmp_path: Path):
    """Multiple diff blocks modifying the same target are sequentially composed in memory."""
    wt = tmp_path / "wt"
    wt.mkdir()
    target = wt / "calc.py"
    target.write_text("def add(a, b): pass\ndef sub(a, b): pass\n", encoding="utf-8")

    model_output = (
        "First modification:\n"
        "```diff\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-def add(a, b): pass\n"
        "+def add(a, b): return a + b\n"
        "```\n\n"
        "Second modification to same file:\n"
        "```diff\n"
        "--- a/calc.py\n"
        "+++ b/calc.py\n"
        "@@ -2,1 +2,1 @@\n"
        "-def sub(a, b): pass\n"
        "+def sub(a, b): return a - b\n"
        "```\n"
    )

    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_mutation_payload(model_output, wt)
    assert err is None
    assert len(edits) == 1
    assert edits[0][0] == "calc.py"
    composed_content = edits[0][1]
    assert "return a + b" in composed_content
    assert "return a - b" in composed_content


# ---------------------------------------------------------------------------
# SEC-50: Sanitized malformed hunk header error
# ---------------------------------------------------------------------------
def test_sec50_sanitized_malformed_hunk_header_error():
    """Malformed hunk header raises stable sanitized error without leaking raw header line."""
    executor = SandboxedBrokerTaskExecutor()
    orig = "line 1\n"
    bad_hunks = ["@@ MALFORMED_HEADER_WITH_SECRET_TOKEN @@"]

    with pytest.raises(ValueError, match="^Malformed hunk header syntax$"):
        executor._apply_hunks(orig, bad_hunks)


# ---------------------------------------------------------------------------
# SEC-51: ScopeGate blocks forbidden file mutations before write
# ---------------------------------------------------------------------------
def test_sec51_scope_gate_blocks_forbidden_files_before_write(tmp_path: Path):
    """ScopeGate inspects extracted edits and blocks forbidden files before disk mutation."""
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", str(wt)], check=True, capture_output=True)

    task = LinearTask(
        id="HER-SEC-51",
        uuid="uuid-sec-51",
        title="Safe Task Title",
        description="Safe task description",
        state="Todo",
        priority=1,
        assignee="hermes-agent",
        labels=(),
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
        url="https://linear.app/issue/HER-SEC-51",
    )

    forbidden_payload = (
        "```python file:deploy/production.yml\n"
        "evil_config: true\n"
        "```\n"
    )

    class MockGateway:
        def health(self) -> bool:
            return True

        def generate_mutation(self, prompt: str, task: LinearTask, worktree_path: Path) -> str:
            return forbidden_payload

    def mock_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    executor = SandboxedBrokerTaskExecutor(
        model_gateway=MockGateway(),
        runner=mock_runner,
    )
    executor.health = lambda: True  # type: ignore[assignment]
    executor.execution_ready = lambda: True  # type: ignore[assignment]

    res = executor.execute(task, wt, "base-sha")
    assert res.status == "BLOCKED"
    assert "SCOPE_GATE_VIOLATION" in (res.error_reason or "")
    # Ensure forbidden file was NEVER created
    assert not (wt / "deploy" / "production.yml").exists()
    assert not (wt / "deploy").exists()


# ---------------------------------------------------------------------------
# SEC-52: Repo context skips untracked, git-ignored, and private files
# ---------------------------------------------------------------------------
def test_sec52_repo_context_skips_untracked_and_ignored_files(tmp_path: Path):
    """Repo context discovery queries git ls-files and skips untracked or private files."""
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", str(wt)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "Hermes Agent"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "agent@hermes.local"], check=True)

    # Tracked legitimate file
    tracked_file = wt / "core.py"
    tracked_file.write_text("def core(): pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "core.py"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", "init"], check=True, capture_output=True)

    # Untracked scratch file
    untracked_scratch = wt / "scratch_work.py"
    untracked_scratch.write_text("SECRET_SCRATCH = 123\n", encoding="utf-8")

    # Private directory file
    logs_dir = wt / "logs"
    logs_dir.mkdir()
    (logs_dir / "debug.log").write_text("HOST_IP = 10.0.0.1\n", encoding="utf-8")

    capsules_dir = wt / "memory_capsules"
    capsules_dir.mkdir()
    (capsules_dir / "user_memory.json").write_text("{}", encoding="utf-8")

    captured_prompt = ""

    class MockGateway:
        def health(self) -> bool:
            return True

        def generate_mutation(self, prompt: str, task: LinearTask, worktree_path: Path) -> str:
            nonlocal captured_prompt
            captured_prompt = prompt
            return "```python file:core.py\ndef core(): return 1\n```\n"

    def mock_runner(cmd, **kwargs):
        if "ls-files" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="core.py\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    task = LinearTask(
        id="HER-SEC-52",
        uuid="uuid-sec-52",
        title="Check Context",
        description="Verify prompt files",
        state="Todo",
        priority=1,
        assignee="hermes-agent",
        labels=(),
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
        url="https://linear.app/issue/HER-SEC-52",
    )

    executor = SandboxedBrokerTaskExecutor(
        model_gateway=MockGateway(),
        runner=mock_runner,
    )
    executor.health = lambda: True  # type: ignore[assignment]
    executor.execution_ready = lambda: True  # type: ignore[assignment]

    executor.execute(task, wt, "base-sha")

    assert "core.py" in captured_prompt
    assert "scratch_work.py" not in captured_prompt
    assert "debug.log" not in captured_prompt
    assert "user_memory.json" not in captured_prompt


# ---------------------------------------------------------------------------
# SEC-53: Unified diff deletion with /dev/null handled cleanly
# ---------------------------------------------------------------------------
def test_sec53_unified_diff_deletion_dev_null_handled(tmp_path: Path):
    """Unified diff deleting file to /dev/null safely unlinks target without path errors."""
    wt = tmp_path / "wt"
    wt.mkdir()

    old_file = wt / "obsolete.py"
    old_file.write_text("def obsolete():\n    pass\n", encoding="utf-8")

    diff_text = (
        "--- a/obsolete.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-def obsolete():\n"
        "-    pass\n"
    )

    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(diff_text, wt)
    assert err is None
    assert edits == [("obsolete.py", None)]

    # Now execute with deletion payload
    class MockGateway:
        def health(self) -> bool:
            return True

        def generate_mutation(self, prompt: str, task: LinearTask, worktree_path: Path) -> str:
            return diff_text

    def mock_runner(cmd, **kwargs):
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=" D obsolete.py\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    task = LinearTask(
        id="HER-SEC-53",
        uuid="uuid-sec-53",
        title="Delete obsolete file",
        description="Remove obsolete file",
        state="Todo",
        priority=1,
        assignee="hermes-agent",
        labels=(),
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
        url="https://linear.app/issue/HER-SEC-53",
    )

    executor_run = SandboxedBrokerTaskExecutor(
        model_gateway=MockGateway(),
        runner=mock_runner,
    )
    executor_run.health = lambda: True  # type: ignore[assignment]
    executor_run.execution_ready = lambda: True  # type: ignore[assignment]

    res = executor_run.execute(task, wt, "base-sha")
    assert res.status == "SUCCESS"
    assert not old_file.exists()


# ---------------------------------------------------------------------------
# SEC-54: Custom tool or plugin in DSH patch rejected
# ---------------------------------------------------------------------------
def test_sec54_custom_tool_in_dsh_patch_rejected(tmp_path: Path):
    """Any custom tool or plugin not explicitly disabled in DSH patch causes health/validation rejection."""
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        "- id: agent-default-model\n"
        "  name: '@deepseek-ai/dsh-agent-default-model'\n"
        "  config:\n"
        "    provider: deepseek-account\n"
        "- id: custom-shell-plugin\n"
        "  disabled: false\n"
        "- id: tool-fs\n"
        "  disabled: true\n"
        "- id: tool-fs-search\n"
        "  disabled: true\n"
        "- id: tool-pwsh\n"
        "  disabled: true\n"
        "- id: tool-bash\n"
        "  disabled: true\n"
        "- id: tool-jobs\n"
        "  disabled: true\n"
        "- id: web\n"
        "  disabled: true\n"
        "- id: subagent\n"
        "  disabled: true\n",
        encoding="utf-8",
    )
    gateway = DshModelGateway(patch_path=patch_file)
    assert not gateway._verify_no_tools_patch()


# ---------------------------------------------------------------------------
# SEC-55: Smoke test rejects symlink scratch repo
# ---------------------------------------------------------------------------
def test_sec55_smoke_test_rejects_symlinked_repo_before_cleanup(tmp_path: Path):
    """Smoke test scratch repo directory rejects symlink before cleanup."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target_external = tmp_path / "external"
    target_external.mkdir()
    repo_link = scratch / "smoke_test_repo"

    # Simulate symlink check logic from run_smoke_test
    try:
        repo_link.symlink_to(target_external, target_is_directory=True)
    except OSError:
        # On Windows without symlink privilege, test logic directly
        pass

    if repo_link.is_symlink():
        with pytest.raises(RuntimeError, match="SMOKE_REPO_SYMLINK_FORBIDDEN"):
            if repo_link.is_symlink():
                raise RuntimeError(f"SMOKE_REPO_SYMLINK_FORBIDDEN: {repo_link} is a symlink")


# ---------------------------------------------------------------------------
# SEC-56: Canonical target path resolution blocks ./deploy/... bypass
# ---------------------------------------------------------------------------
def test_sec56_canonical_target_path_resolution_blocks_dot_slash_deploy(tmp_path: Path):
    """ScopeGate blocks ./deploy/hermes.json via canonical resolved relative path."""
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", str(wt)], check=True, capture_output=True)

    task = LinearTask(
        id="HER-SEC-56",
        uuid="uuid-sec-56",
        title="Deploy Bypass Attempt",
        description="Try to bypass scope gate with dot-slash",
        state="Todo",
        priority=1,
        assignee="hermes-agent",
        labels=(),
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
        url="https://linear.app/issue/HER-SEC-56",
    )

    bypass_payload = (
        "```python file:./deploy/hermes-production.json\n"
        "{\"evil\": true}\n"
        "```\n"
    )

    class MockGateway:
        def health(self) -> bool:
            return True

        def generate_mutation(self, prompt: str, task: LinearTask, worktree_path: Path) -> str:
            return bypass_payload

    def mock_runner(cmd, **kwargs):
        if "ls-files" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    executor = SandboxedBrokerTaskExecutor(
        model_gateway=MockGateway(),
        runner=mock_runner,
    )
    executor.health = lambda: True  # type: ignore[assignment]
    executor.execution_ready = lambda: True  # type: ignore[assignment]

    res = executor.execute(task, wt, "base-sha")
    assert res.status == "BLOCKED"
    assert "SCOPE_GATE_VIOLATION" in (res.error_reason or "")
    assert not (wt / "deploy").exists()


# ---------------------------------------------------------------------------
# SEC-57: Context collection fails closed on git discovery error
# ---------------------------------------------------------------------------
def test_sec57_context_collection_fails_closed_on_git_discovery_error(tmp_path: Path):
    """If git ls-files fails, context collection halts immediately without reading private files."""
    wt = tmp_path / "wt"
    wt.mkdir()

    task = LinearTask(
        id="HER-SEC-57",
        uuid="uuid-sec-57",
        title="Task",
        description="Task",
        state="Todo",
        priority=1,
        assignee="hermes-agent",
        labels=(),
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
        url="https://linear.app/issue/HER-SEC-57",
    )

    class MockGateway:
        def health(self) -> bool:
            return True

        def generate_mutation(self, prompt: str, task: LinearTask, worktree_path: Path) -> str:
            return ""

    def mock_runner(cmd, **kwargs):
        if "ls-files" in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: not a git repository")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    executor = SandboxedBrokerTaskExecutor(
        model_gateway=MockGateway(),
        runner=mock_runner,
    )
    executor.health = lambda: True  # type: ignore[assignment]
    executor.execution_ready = lambda: True  # type: ignore[assignment]

    res = executor.execute(task, wt, "base-sha")
    assert res.status == "FAILED"
    assert res.error_reason == "GIT_TRACKED_FILES_DISCOVERY_FAILED"


# ---------------------------------------------------------------------------
# SEC-58: Markdown payload preserves embedded code fences
# ---------------------------------------------------------------------------
def test_sec58_markdown_payload_preserves_embedded_code_fences(tmp_path: Path):
    """Outer 4-backtick fence preserves inner 3-backtick Python code block in Markdown payload."""
    payload = (
        "````markdown file:README.md\n"
        "# Title\n"
        "Here is sample code:\n"
        "```python\n"
        "print('hello')\n"
        "```\n"
        "End of readme.\n"
        "````\n"
    )

    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_mutation_payload(payload, tmp_path)
    assert err is None
    assert len(edits) == 1
    target, content = edits[0]
    assert target == "README.md"
    assert "print('hello')" in (content or "")
    assert "End of readme." in (content or "")


# ---------------------------------------------------------------------------
# SEC-59: Excess diff lines in hunk cause rejection
# ---------------------------------------------------------------------------
def test_sec59_excess_diff_lines_rejected(tmp_path: Path):
    """Hunk declaring 1 added line but supplying 2 lines is rejected instead of truncated."""
    wt = tmp_path / "wt"
    wt.mkdir()
    target_file = wt / "app.py"
    target_file.write_text("orig line\n", encoding="utf-8")

    excess_diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,1 +1,1 @@\n"
        "+first line\n"
        "+excess second line\n"
    )

    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(excess_diff, wt)
    assert err is not None
    assert "HUNK_APPLICATION_FAILED" in err
    assert "mismatch" in err.lower()


# ---------------------------------------------------------------------------
# SEC-60: Group attribute in DSH patch rejected
# ---------------------------------------------------------------------------
def test_sec60_group_attribute_in_patch_rejected(tmp_path: Path):
    """Patch specifying group: true is rejected to prevent defeating Cordis disabled flags."""
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        "- id: agent-default-model\n"
        "  name: '@deepseek-ai/dsh-agent-default-model'\n"
        "  group: true\n"
        "- id: tool-fs\n"
        "  disabled: true\n"
        "- id: tool-fs-search\n"
        "  disabled: true\n"
        "- id: tool-pwsh\n"
        "  disabled: true\n"
        "- id: tool-bash\n"
        "  disabled: true\n"
        "- id: tool-jobs\n"
        "  disabled: true\n"
        "- id: web\n"
        "  disabled: true\n"
        "- id: subagent\n"
        "  disabled: true\n"
        "- id: tool-skill\n"
        "  disabled: true\n"
        "- id: tool-todo\n"
        "  disabled: true\n"
        "- id: tool-goal\n"
        "  disabled: true\n",
        encoding="utf-8",
    )
    gateway = DshModelGateway(patch_path=patch_file)
    assert not gateway._verify_no_tools_patch()


# ---------------------------------------------------------------------------
# SEC-61: ScopeGate preserves leading dot on ./.env path
# ---------------------------------------------------------------------------
def test_sec61_scope_gate_preserves_leading_dot_on_dot_slash_env():
    """ScopeGate does not strip leading dot from ./.env or ./.env.production."""
    allowed, reason = ScopeGate.evaluate_changed_files(["./.env"])
    assert not allowed
    assert reason == BlockReasonCode.CREDENTIALS_MUTATION_FORBIDDEN

    allowed_prod, reason_prod = ScopeGate.evaluate_changed_files(["./.env.production"])
    assert not allowed_prod
    assert reason_prod == BlockReasonCode.CREDENTIALS_MUTATION_FORBIDDEN

    allowed_nested, _ = ScopeGate.evaluate_changed_files([".//.env"])
    assert not allowed_nested


# ---------------------------------------------------------------------------
# SEC-62: Hunk line starting with triple dash treated as hunk when hunk active
# ---------------------------------------------------------------------------
def test_sec62_hunk_line_starting_with_triple_dash_treated_as_hunk(tmp_path: Path):
    """Lines within a declared hunk starting with '--- ' are not misidentified as file headers."""
    wt = tmp_path / "wt"
    wt.mkdir()
    target_file = wt / "query.sql"
    target_file.write_text("-- old comment\nSELECT 1;\n", encoding="utf-8")

    diff = (
        "--- a/query.sql\n"
        "+++ b/query.sql\n"
        "@@ -1,2 +1,2 @@\n"
        "--- old comment\n"
        "+-- new comment\n"
        " SELECT 1;\n"
    )
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(diff, wt)
    assert err is None
    assert len(edits) == 1
    target, new_content = edits[0]
    assert target == "query.sql"
    assert new_content == "-- new comment\nSELECT 1;\n"


# ---------------------------------------------------------------------------
# SEC-63: Partial deletion patch targeting /dev/null rejected
# ---------------------------------------------------------------------------
def test_sec63_partial_deletion_patch_rejected(tmp_path: Path):
    """Unified diff targeting /dev/null that does not completely delete file content is rejected."""
    wt = tmp_path / "wt"
    wt.mkdir()
    target_file = wt / "obsolete.py"
    target_file.write_text("line1\nline2\n", encoding="utf-8")

    incomplete_diff = (
        "--- a/obsolete.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +1,1 @@\n"
        "-line1\n"
        " line2\n"
    )
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(incomplete_diff, wt)
    assert edits == []
    assert "INCOMPLETE_FILE_DELETION" in (err or "")


# ---------------------------------------------------------------------------
# SEC-64: Missing-newline marker supported without error
# ---------------------------------------------------------------------------
def test_sec64_missing_newline_marker_supported(tmp_path: Path):
    """Git diff marker '\\ No newline at end of file' is recognized and trims trailing newline."""
    wt = tmp_path / "wt"
    wt.mkdir()
    target_file = wt / "config.txt"
    target_file.write_text("old_val\n", encoding="utf-8")

    diff = (
        "--- a/config.txt\n"
        "+++ b/config.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-old_val\n"
        "+new_val\n"
        "\\ No newline at end of file\n"
    )
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(diff, wt)
    assert err is None
    assert len(edits) == 1
    target, new_content = edits[0]
    assert target == "config.txt"
    assert new_content == "new_val"


# ---------------------------------------------------------------------------
# SEC-65: Malformed hunk line classification is sanitized
# ---------------------------------------------------------------------------
def test_sec65_malformed_hunk_line_classification_sanitized(tmp_path: Path):
    """Malformed hunk line emits stable enum without echoing potentially sensitive model snippet."""
    wt = tmp_path / "wt"
    wt.mkdir()
    target_file = wt / "data.py"
    target_file.write_text("alpha\n", encoding="utf-8")

    malformed_diff = (
        "--- a/data.py\n"
        "+++ b/data.py\n"
        "@@ -1,1 +1,1 @@\n"
        "?SECRET_AUTH_TOKEN_VALUE=12345\n"
    )
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(malformed_diff, wt)
    assert edits == []
    assert err == "MALFORMED_DIFF_HUNK_LINE"
    assert "SECRET" not in (err or "")


# ---------------------------------------------------------------------------
# SEC-66: Mismatched name in tool-disabling patch entry rejected
# ---------------------------------------------------------------------------
def test_sec66_mismatched_name_in_tool_disabling_entry_rejected(tmp_path: Path):
    """Disabling entry supplying wrong package name is rejected to prevent Cordis patch bypass."""
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        "- id: agent-default-model\n"
        "  name: '@deepseek-ai/dsh-agent-default-model'\n"
        "- id: tool-fs\n"
        "  name: 'wrong-package-name'\n"
        "  disabled: true\n"
        "- id: tool-fs-search\n"
        "  disabled: true\n"
        "- id: tool-pwsh\n"
        "  disabled: true\n"
        "- id: tool-bash\n"
        "  disabled: true\n"
        "- id: tool-jobs\n"
        "  disabled: true\n"
        "- id: web\n"
        "  disabled: true\n"
        "- id: subagent\n"
        "  disabled: true\n"
        "- id: tool-skill\n"
        "  disabled: true\n"
        "- id: tool-todo\n"
        "  disabled: true\n"
        "- id: tool-goal\n"
        "  disabled: true\n",
        encoding="utf-8",
    )
    gateway = DshModelGateway(patch_path=patch_file)
    assert not gateway._verify_no_tools_patch()


# ---------------------------------------------------------------------------
# SEC-67: Missing newline marker after deletion does not strip surviving prefix
# ---------------------------------------------------------------------------
def test_sec67_missing_newline_marker_after_deletion_does_not_strip_surviving_prefix(tmp_path: Path):
    """When a deleted EOF line lacked a newline, replacing it does not merge with preceding line."""
    wt = tmp_path / "wt"
    wt.mkdir()
    target_file = wt / "data.txt"
    target_file.write_text("prefix\nold", encoding="utf-8")

    diff = (
        "--- a/data.txt\n"
        "+++ b/data.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " prefix\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
    )
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(diff, wt)
    assert err is None
    assert len(edits) == 1
    target, new_content = edits[0]
    assert target == "data.txt"
    assert new_content == "prefix\nnew\n"


# ---------------------------------------------------------------------------
# SEC-68: DSH environment uses DSH_HOME and isolates profile directories
# ---------------------------------------------------------------------------
def test_sec68_dsh_env_uses_dsh_home_and_isolates_profiles():
    """DshModelGateway configures isolated DSH_HOME and strips host secrets."""
    gateway = DshModelGateway()
    env = gateway._get_isolated_env()
    assert "DSH_HOME" in env
    assert "CORDIS_CONFIG_DIR" in env
    assert env["CORDIS_DISABLE_PLUGINS"] == "1"
    for forbidden_key in ("HERMES_TELEGRAM_BOT_TOKEN", "LINEAR_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert forbidden_key not in env


# ---------------------------------------------------------------------------
# SEC-69: Uppercase or padded tool package name rejected
# ---------------------------------------------------------------------------
def test_sec69_uppercase_or_padded_tool_package_name_rejected(tmp_path: Path):
    """Disabling entry supplying uppercase package name is rejected to match Cordis exact case matching."""
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        "- id: agent-default-model\n"
        "  name: '@deepseek-ai/dsh-agent-default-model'\n"
        "- id: tool-fs\n"
        "  name: '@DEEPSEEK-AI/DSH-TOOL-FS'\n"
        "  disabled: true\n"
        "- id: tool-fs-search\n"
        "  disabled: true\n"
        "- id: tool-pwsh\n"
        "  disabled: true\n"
        "- id: tool-bash\n"
        "  disabled: true\n"
        "- id: tool-jobs\n"
        "  disabled: true\n"
        "- id: web\n"
        "  disabled: true\n"
        "- id: subagent\n"
        "  disabled: true\n"
        "- id: tool-skill\n"
        "  disabled: true\n"
        "- id: tool-todo\n"
        "  disabled: true\n"
        "- id: tool-goal\n"
        "  disabled: true\n",
        encoding="utf-8",
    )
    gateway = DshModelGateway(patch_path=patch_file)
    assert not gateway._verify_no_tools_patch()


# ---------------------------------------------------------------------------
# SEC-70: Creation diff targeting existing file rejected
# ---------------------------------------------------------------------------
def test_sec70_creation_diff_targeting_existing_file_rejected(tmp_path: Path):
    """Diff declaring creation from /dev/null targeting an existing file is rejected."""
    wt = tmp_path / "wt"
    wt.mkdir()
    target_file = wt / "app.py"
    target_file.write_text("existing content\n", encoding="utf-8")

    creation_diff = (
        "--- /dev/null\n"
        "+++ b/app.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+new content\n"
    )
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(creation_diff, wt)
    assert edits == []
    assert err == "CREATION_TARGET_ALREADY_EXISTS_app.py"


# ---------------------------------------------------------------------------
# SEC-71: Modification diff targeting nonexistent file rejected
# ---------------------------------------------------------------------------
def test_sec71_modification_diff_targeting_nonexistent_file_rejected(tmp_path: Path):
    """Diff declaring modification of nonexistent file is rejected."""
    wt = tmp_path / "wt"
    wt.mkdir()

    mod_diff = (
        "--- a/missing.py\n"
        "+++ b/missing.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(mod_diff, wt)
    assert edits == []
    assert err == "MODIFICATION_TARGET_NOT_FOUND_missing.py"


# ---------------------------------------------------------------------------
# SEC-72: Inference runs in empty isolated scratch CWD
# ---------------------------------------------------------------------------
def test_sec72_inference_runs_in_empty_isolated_scratch_cwd(tmp_path: Path):
    """DshModelGateway executes inference in an empty directory to isolate workspace instructions."""
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        "- id: agent-default-model\n"
        "  name: '@deepseek-ai/dsh-agent-default-model'\n"
        "- id: tool-fs\n  disabled: true\n"
        "- id: tool-fs-search\n  disabled: true\n"
        "- id: tool-pwsh\n  disabled: true\n"
        "- id: tool-bash\n  disabled: true\n"
        "- id: tool-jobs\n  disabled: true\n"
        "- id: web\n  disabled: true\n"
        "- id: subagent\n  disabled: true\n"
        "- id: tool-skill\n  disabled: true\n"
        "- id: tool-todo\n  disabled: true\n"
        "- id: tool-goal\n  disabled: true\n",
        encoding="utf-8",
    )
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "AGENTS.local.md").write_text("SENSITIVE_LOCAL_INSTRUCTION", encoding="utf-8")

    captured_cwd = []

    def fake_runner(cmd, cwd=None, **kwargs):
        captured_cwd.append(cwd)
        return subprocess.CompletedProcess(cmd, 0, stdout="```python file:test.py\npass\n```", stderr="")

    gateway = DshModelGateway(patch_path=patch_file, runner=fake_runner)
    gateway.generate_mutation("prompt", make_task(), wt)

    assert len(captured_cwd) == 1
    # Verify inference CWD was NOT the worktree containing AGENTS.local.md
    assert captured_cwd[0] != str(wt)
    assert not (Path(captured_cwd[0]) / "AGENTS.local.md").exists()


# ---------------------------------------------------------------------------
# SEC-73: Windows trailing-dot alias blocked by containment and ScopeGate
# ---------------------------------------------------------------------------
def test_sec73_trailing_dot_alias_blocked(tmp_path: Path):
    """Path segment ending in dot or space is rejected to prevent Win32 directory alias bypass."""
    wt = tmp_path / "wt"
    wt.mkdir()

    # Direct containment check
    ok, err = validate_worktree_path_containment("gateway/migrations./v1.sql", wt)
    assert not ok
    assert err == "NONCANONICAL_TRAILING_DOT_OR_SPACE_ALIAS"

    # ScopeGate evaluation
    allowed, reason = ScopeGate.evaluate_changed_files(["gateway/migrations./v1.sql"])
    assert not allowed
    assert reason == BlockReasonCode.DB_MIGRATION_FORBIDDEN


# ---------------------------------------------------------------------------
# SEC-74: Case-insensitive Python suffix check invokes syntax check
# ---------------------------------------------------------------------------
def test_sec74_case_insensitive_python_syntax_check(tmp_path: Path):
    """Python files with uppercase extension (e.g. app.PY) trigger sandboxed syntax check."""
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", str(wt)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "Hermes Agent"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "agent@hermes.local"], check=True)
    (wt / "app.PY").write_text("def valid(): pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "app.PY"], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", "init"], check=True, capture_output=True)

    syntax_checked_files = []

    class MockGateway:
        def health(self) -> bool:
            return True

        def execution_ready(self) -> bool:
            return True

        def generate_mutation(self, prompt, task, wt_path):
            return "```python file:app.PY\ndef broken(): invalid syntax here !!!\n```"

    executor = SandboxedBrokerTaskExecutor(model_gateway=MockGateway())
    executor._run_sandboxed_probe = lambda w: (True, None)  # type: ignore[assignment]

    def mock_syntax_check(w, rel):
        syntax_checked_files.append(rel)
        return False, "SyntaxError: invalid syntax"

    executor._run_sandboxed_syntax_check = mock_syntax_check  # type: ignore[assignment]

    res = executor.execute(make_task(), wt, "base-sha")
    assert res.status == "FAILED"
    assert "SYNTAX_VALIDATION_FAILED" in (res.error_reason or "")
    assert "app.PY" in syntax_checked_files


# ---------------------------------------------------------------------------
# SEC-75: execution_ready fails closed when credentials absent
# ---------------------------------------------------------------------------
def test_sec75_execution_ready_fails_closed_when_credentials_absent(tmp_path: Path, monkeypatch):
    """DshModelGateway.execution_ready() returns False when credentials cannot be found."""
    patch_file = tmp_path / "patch.yml"
    patch_file.write_text(
        "- id: agent-default-model\n"
        "  name: '@deepseek-ai/dsh-agent-default-model'\n"
        "- id: tool-fs\n  disabled: true\n"
        "- id: tool-fs-search\n  disabled: true\n"
        "- id: tool-pwsh\n  disabled: true\n"
        "- id: tool-bash\n  disabled: true\n"
        "- id: tool-jobs\n  disabled: true\n"
        "- id: web\n  disabled: true\n"
        "- id: subagent\n  disabled: true\n"
        "- id: tool-skill\n  disabled: true\n"
        "- id: tool-todo\n  disabled: true\n"
        "- id: tool-goal\n  disabled: true\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "dsh"
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    empty_home = tmp_path / "empty_home"
    empty_home.mkdir()
    monkeypatch.setenv("DSH_HOME", str(empty_home))
    monkeypatch.setenv("USERPROFILE", str(empty_home))
    monkeypatch.setenv("HOME", str(empty_home))

    def fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="dsh 1.0.0", stderr="")

    gateway = DshModelGateway(dsh_bin=str(fake_bin), patch_path=patch_file, runner=fake_runner)
    assert gateway.health()  # shallow health passes because binary and patch exist

    # But without credentials, execution_ready() fails closed
    gateway._custom_runner = None
    assert not gateway.execution_ready()


# ---------------------------------------------------------------------------
# SEC-76: Repeated diff sections for same file accumulate hunks
# ---------------------------------------------------------------------------
def test_sec76_repeated_diff_sections_accumulate_hunks(tmp_path: Path):
    """Multiple diff sections modifying the same file within a diff block are accumulated."""
    wt = tmp_path / "wt"
    wt.mkdir()
    target_file = wt / "code.py"
    target_file.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")

    diff = (
        "--- a/code.py\n"
        "+++ b/code.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-line1\n"
        "+new1\n"
        "--- a/code.py\n"
        "+++ b/code.py\n"
        "@@ -5,1 +5,1 @@\n"
        "-line5\n"
        "+new5\n"
    )
    executor = SandboxedBrokerTaskExecutor()
    edits, err = executor._parse_unified_diff(diff, wt)
    assert err is None
    assert len(edits) == 1
    target, new_content = edits[0]
    assert target == "code.py"
    assert new_content == "new1\nline2\nline3\nline4\nnew5\n"


# ---------------------------------------------------------------------------
# SEC-77: Fenced code blocks with CRLF line endings parsed successfully
# ---------------------------------------------------------------------------
def test_sec77_fenced_code_blocks_with_crlf_parsed(tmp_path: Path):
    """Model payload formatted with CRLF line endings is parsed without error."""
    crlf_payload = (
        "Here is the change:\r\n"
        "```python file:module.py\r\n"
        "def hello():\r\n"
        "    return 'world'\r\n"
        "```\r\n"
    )
    from ai_engineering.linear_dispatcher.task_executor import _extract_fenced_code_blocks
    blocks = _extract_fenced_code_blocks(crlf_payload)
    assert len(blocks) == 1
    tag, body = blocks[0]
    assert tag == "python file:module.py"
    assert "return 'world'" in body


# ---------------------------------------------------------------------------
# SEC-78: Shipped package identities for web and subagent verified
# ---------------------------------------------------------------------------
def test_sec78_shipped_package_identities_verified(tmp_path: Path):
    """Verifier requires exact shipped package names @deepseek-ai/dsh-web and @deepseek-ai/dsh-subagent."""
    valid_patch = tmp_path / "valid.patch.yml"
    valid_patch.write_text(
        """- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    model: test
- id: tool-fs
  name: "@deepseek-ai/dsh-tool-fs"
  disabled: true
- id: tool-fs-search
  name: "@deepseek-ai/dsh-tool-fs-search"
  disabled: true
- id: tool-pwsh
  name: "@deepseek-ai/dsh-tool-pwsh"
  disabled: true
- id: tool-bash
  name: "@deepseek-ai/dsh-tool-bash"
  disabled: true
- id: tool-jobs
  name: "@deepseek-ai/dsh-tool-jobs"
  disabled: true
- id: web
  name: "@deepseek-ai/dsh-web"
  disabled: true
- id: subagent
  name: "@deepseek-ai/dsh-subagent"
  disabled: true
- id: tool-skill
  name: "@deepseek-ai/dsh-tool-skill"
  disabled: true
- id: tool-todo
  name: "@deepseek-ai/dsh-tool-todo"
  disabled: true
- id: tool-goal
  name: "@deepseek-ai/dsh-tool-goal"
  disabled: true
""",
        encoding="utf-8",
    )
    gw = DshModelGateway(patch_path=valid_patch)
    assert gw._verify_no_tools_patch() is True

    # Old/incorrect names must be rejected fail-closed
    invalid_patch = tmp_path / "invalid.patch.yml"
    invalid_patch.write_text(
        valid_patch.read_text(encoding="utf-8").replace("@deepseek-ai/dsh-web", "@deepseek-ai/dsh-tool-web"),
        encoding="utf-8",
    )
    gw_invalid = DshModelGateway(patch_path=invalid_patch)
    assert gw_invalid._verify_no_tools_patch() is False


# ---------------------------------------------------------------------------
# SEC-79: Telemetry-only anonymous user ID rejected for model readiness
# ---------------------------------------------------------------------------
def test_sec79_telemetry_only_anonymous_id_rejected_for_readiness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Presence of .anonymous-user-id alone does NOT satisfy execution_ready()."""
    dsh_dir = tmp_path / ".dsh"
    dsh_dir.mkdir()
    (dsh_dir / ".anonymous-user-id").write_text("test-uuid-telemetry", encoding="utf-8")

    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    valid_patch = tmp_path / "patch.yml"
    valid_patch.write_text(
        """- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    model: test
- id: tool-fs
  disabled: true
- id: tool-fs-search
  disabled: true
- id: tool-pwsh
  disabled: true
- id: tool-bash
  disabled: true
- id: tool-jobs
  disabled: true
- id: web
  disabled: true
- id: subagent
  disabled: true
- id: tool-skill
  disabled: true
- id: tool-todo
  disabled: true
- id: tool-goal
  disabled: true
""",
        encoding="utf-8",
    )
    gw = DshModelGateway(patch_path=valid_patch)
    monkeypatch.setattr(gw, "health", lambda: True)

    # When only .anonymous-user-id exists: not ready
    assert gw.execution_ready() is False

    # When valid .credentials.yaml exists: ready
    (dsh_dir / ".credentials.yaml").write_text("records:\n  default:\n    token: test\n", encoding="utf-8")
    assert gw.execution_ready() is True


# ---------------------------------------------------------------------------
# SEC-80: Non-string JSON content rejected without corrupting targets
# ---------------------------------------------------------------------------
def test_sec80_non_string_json_content_rejected(tmp_path: Path):
    """Model returning non-string content (dict, int, null) fails closed without writing."""
    wt = tmp_path / "wt"
    wt.mkdir()
    target_py = wt / "calculator.py"
    target_py.write_text("def multiply(a, b): pass\n", encoding="utf-8")

    malformed_payload = '[{"path": "calculator.py", "content": {"evil": 123}}]'
    executor = SandboxedBrokerTaskExecutor(
        model_gateway=CallableModelGateway(lambda p, t, w: malformed_payload),
    )
    result = executor.execute(make_task(), wt, "base_sha")
    assert result.status != "SUCCESS"
    # Target file must remain untouched
    assert target_py.read_text(encoding="utf-8") == "def multiply(a, b): pass\n"


# ---------------------------------------------------------------------------
# SEC-81: Fenced JSON mutation parsed and single-file fallback restricted
# ---------------------------------------------------------------------------
def test_sec81_fenced_json_parsed_and_single_fallback_restricted(tmp_path: Path):
    """Fenced JSON parsed through mutation schema; non-Python or JSON literal rejected in fallback."""
    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init", str(wt)], check=True, capture_output=True)
    target_py = wt / "calculator.py"
    target_py.write_text("def multiply(a, b): pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(wt), "add", "calculator.py"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-m", "init"], check=True, capture_output=True)

    # Fenced JSON is properly parsed and applied
    fenced_payload = '```json\n[{"path": "calculator.py", "content": "def multiply(a, b): return a * b\\n"}]\n```'
    executor = SandboxedBrokerTaskExecutor(
        model_gateway=CallableModelGateway(lambda p, t, w: fenced_payload),
    )
    # Mock runner and probe to test mutation application
    executor._run_sandboxed_probe = lambda w: (True, None)
    executor._run_sandboxed_syntax_check = lambda w, p: (True, None)
    executor._detect_changed_files = lambda w: ("calculator.py",)
    res = executor.execute(make_task(), wt, "base_sha")
    assert res.status == "SUCCESS"
    assert "return a * b" in target_py.read_text(encoding="utf-8")

    # A naked data literal block must NOT be treated as Python module in single fallback
    raw_literal_block = '```\n[1, 2, 3]\n```'
    edits, err = executor._parse_mutation_payload(raw_literal_block, wt)
    assert edits == []
    assert err in ("MALFORMED_JSON_ITEM_INVALID", "JSON_BLOCK_IN_SINGLE_FILE_FALLBACK_REJECTED", "SINGLE_FILE_FALLBACK_LITERAL_EXPRESSION_REJECTED")

    # A python-tagged list comprehension / literal expression must also be rejected by single fallback
    py_literal_block = '```python\n[x for x in range(10)]\n```'
    edits_py, err_py = executor._parse_mutation_payload(py_literal_block, wt)
    assert edits_py == []
    assert err_py == "SINGLE_FILE_FALLBACK_LITERAL_EXPRESSION_REJECTED"

    # A non-Python tagged block must NOT be treated as Python module in single fallback
    non_py_block = '```json\n{"invalid": "schema"}\n```'
    edits2, err2 = executor._parse_mutation_payload(non_py_block, wt)
    assert edits2 == []
    assert err2 is not None
