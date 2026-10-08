"""Hermes Safe Executor Stage G Functional Smoke Test.

Executes a real end-to-end task against an isolated scratch Git repository:
1. Real AI model inference via Model Gateway
2. Declarative mutation generation
3. Strict path containment validation
4. Sandboxed syntax validation
5. Evidence bundle generation and external filesystem verification
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from ai_engineering.linear_dispatcher.contracts import LinearTask
from ai_engineering.linear_dispatcher.task_executor import (
    DshModelGateway,
    SandboxedBrokerTaskExecutor,
)


def run_smoke_test(scratch_path: Path | None = None) -> dict:
    if scratch_path:
        scratch_dir = Path(scratch_path).resolve()
    elif "HERMES_SMOKE_SCRATCH_DIR" in os.environ:
        scratch_dir = Path(os.environ["HERMES_SMOKE_SCRATCH_DIR"]).resolve()
    else:
        default_dir = Path(r"C:\Users\Oleg\.gemini\antigravity\brain\01653d68-a368-4bb7-bdb1-d3a09e5abb31\scratch")
        scratch_dir = default_dir if default_dir.exists() else (Path(__file__).resolve().parent.parent / ".smoke_scratch")
    scratch_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = scratch_dir / "smoke_test_repo"
    evidence_file = scratch_dir / "smoke_test_evidence.json"
    sentinel_external_file = scratch_dir / "external_sentinel.txt"
    patch_path = scratch_dir / "pure_inference.patch.yml"

    # Pre-write validation: reject any symlinks, hardlinks, or traversal escapes in output targets
    for p in (sentinel_external_file, evidence_file, patch_path, repo_dir):
        if p.is_symlink():
            raise RuntimeError(f"SMOKE_OUTPUT_PATH_SYMLINK_FORBIDDEN: {p}")
        try:
            p.resolve().relative_to(scratch_dir.resolve())
        except ValueError:
            raise RuntimeError(f"SMOKE_OUTPUT_PATH_ESCAPE_DETECTED: {p} resolves outside {scratch_dir}")
        if p.exists() and p.is_file():
            st = p.stat()
            if hasattr(st, "st_nlink") and st.st_nlink > 1:
                raise RuntimeError(f"SMOKE_OUTPUT_PATH_HARDLINK_FORBIDDEN: {p}")

    # Set up external sentinel to prove external filesystem remains untouched
    sentinel_external_file.write_text("SENTINEL_UNTOUCHED_STATE", encoding="utf-8")
    initial_sentinel_mtime = sentinel_external_file.stat().st_mtime

    # Clean and reinitialize scratch git repository
    if repo_dir.exists():
        if repo_dir.is_symlink():
            raise RuntimeError(f"SMOKE_REPO_SYMLINK_FORBIDDEN: {repo_dir} is a symlink")
        try:
            repo_dir.resolve().relative_to(scratch_dir.resolve())
        except ValueError:
            raise RuntimeError(f"SMOKE_REPO_ESCAPE_DETECTED: {repo_dir} resolves outside {scratch_dir}")

        def _handle_remove_readonly(func, path, exc_info):
            import stat
            try:
                os.chmod(path, stat.S_IWRITE)
                func(path)
            except Exception:
                raise exc_info[1]

        shutil.rmtree(repo_dir, onerror=_handle_remove_readonly)
    repo_dir.mkdir(parents=True, exist_ok=True)

    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)
    calc_file = repo_dir / "calculator.py"
    calc_file.write_text(
        "def multiply(a: int, b: int) -> int:\n    \"\"\"Multiply two integers.\"\"\"\n    pass\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo_dir), "add", "calculator.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-m", "Initial commit"],
        check=True,
        capture_output=True,
    )

    base_sha = (
        subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
    )

    task = LinearTask(
        id="HER-SMOKE-01",
        uuid="uuid-smoke-01",
        title="Implement multiply function",
        description="Implement multiply(a: int, b: int) -> int in calculator.py so that it returns a * b.",
        state="Todo",
        priority=1,
        assignee="hermes-agent",
        labels=("agent-auto",),
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
        url="https://linear.app/issue/HER-SMOKE-01",
    )

    # Use no-tools patch for pure inference DSH gateway
    patch_path = scratch_dir / "pure_inference.patch.yml"
    patch_path.write_text(
        """- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: deepseek-account
    model: deepseek-v4-pro
    reasoningEffort: low
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

    gateway = DshModelGateway(patch_path=patch_path, timeout_sec=90)
    executor = SandboxedBrokerTaskExecutor(
        model_gateway=gateway,
        distro="Ubuntu",
        bwrap_bin="bwrap",
        timeout_sec=120,
    )

    print(f"[*] Starting Smoke Test with executor health={executor.health()}, ready={executor.execution_ready()}")
    start_time = time.time()
    result = executor.execute(task, repo_dir, base_sha)
    elapsed = time.time() - start_time

    # Verify external sentinel is untouched
    current_sentinel_mtime = sentinel_external_file.stat().st_mtime
    sentinel_untouched = (
        sentinel_external_file.read_text(encoding="utf-8") == "SENTINEL_UNTOUCHED_STATE"
        and current_sentinel_mtime == initial_sentinel_mtime
    )

    # Verify multiplication behavior functionally strictly INSIDE Bubblewrap sandbox
    linux_wt = executor._to_linux_path(repo_dir)
    test_script = (
        "import sys\n"
        f"sys.path.insert(0, '{linux_wt}')\n"
        "import calculator\n"
        "assert calculator.multiply(3, 4) == 12, f'Expected 12, got {calculator.multiply(3, 4)}'\n"
        "assert calculator.multiply(-2, 5) == -10, f'Expected -10, got {calculator.multiply(-2, 5)}'\n"
        "assert calculator.multiply(0, 99) == 0, f'Expected 0, got {calculator.multiply(0, 99)}'\n"
        "print('BEHAVIOR_VERIFIED_PASS')\n"
    )
    bwrap_cmd = executor.build_bwrap_command(
        linux_wt,
        ["python3", "-c", test_script],
    )
    full_sandboxed_cmd = (
        ["wsl.exe", "-d", executor.distro, "--"] + bwrap_cmd
        if executor.is_windows
        else bwrap_cmd
    )
    test_res = subprocess.run(
        full_sandboxed_cmd,
        capture_output=True,
        text=True,
        timeout=15,
    )
    functional_verification = (
        test_res.returncode == 0 and "BEHAVIOR_VERIFIED_PASS" in test_res.stdout
    )
    new_code = calc_file.read_text(encoding="utf-8")

    evidence = {
        "status": result.status,
        "error_reason": result.error_reason,
        "raw_evidence": result.evidence,
        "changed_files": list(result.changed_files),
        "execution_id": result.execution_id,
        "executor_name": result.executor_name,
        "duration_sec": round(elapsed, 2),
        "base_sha": base_sha,
        "sentinel_untouched": sentinel_untouched,
        "functional_verification": functional_verification,
        "functional_test_stdout": test_res.stdout.strip(),
        "new_calculator_content": new_code,
    }

    evidence_file.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(f"[+] Smoke Test Result: {result.status}")
    print(f"[+] Changed files: {result.changed_files}")
    print(f"[+] Functional verification: {functional_verification}")
    print(f"[+] Sentinel untouched: {sentinel_untouched}")
    print(f"[+] Evidence written to: {evidence_file}")
    return evidence


if __name__ == "__main__":
    ev = run_smoke_test()
    if ev["status"] != "SUCCESS" or not ev["functional_verification"] or not ev["sentinel_untouched"]:
        sys.exit(1)
    sys.exit(0)
