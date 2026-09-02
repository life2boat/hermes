"""PR-13 read-only separation tests: AST scans + state immutability."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.ai_engineering.runtime_fixture_helpers import (
    RUN_ID,
    WORKSPACE_ID,
    execute_default,
    make_local_fixture,
)

RUNTIME_PACKAGE = Path(__file__).resolve().parents[2] / "ai_engineering" / "runtime"

_FORBIDDEN_MODULES = (
    "socket",
    "http",
    "urllib",
    "urllib.request",
    "requests",
    "httpx",
    "aiohttp",
    "paramiko",
    "asyncssh",
    "pickle",
    "shelve",
    "telnetlib",
    "ftplib",
    "smtplib",
)

_FORBIDDEN_CALLS = ("eval", "exec", "compile", "__import__", "system", "popen")

_CONTROL_PLANE_MUTATORS = (
    "record_event",
    "record_candidate_completed",
    "record_candidate_results",
    "record_judgement",
    "record_validation",
    "record_requalification_results",
    "trigger_requalification",
    "generate_handoff",
    "apply_event",
    "request_cancel_control",
)


def _runtime_sources():
    return sorted(RUNTIME_PACKAGE.glob("*.py"))


def test_runtime_package_exists_with_modules():
    names = {p.name for p in _runtime_sources()}
    assert "__init__.py" in names
    assert "runtime_contracts.py" in names
    assert "spawn_gate.py" in names


def test_no_forbidden_module_imports():
    for path in _runtime_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root not in {m.split(".")[0] for m in _FORBIDDEN_MODULES}, (
                        f"{path.name}: forbidden import {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                root = module.split(".")[0]
                assert root not in {m.split(".")[0] for m in _FORBIDDEN_MODULES}, (
                    f"{path.name}: forbidden import from {module}"
                )


def test_no_forbidden_builtin_calls():
    for path in _runtime_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in _FORBIDDEN_CALLS, (
                    f"{path.name}: forbidden call {node.func.id}"
                )


def test_no_shell_true_anywhere():
    for path in _runtime_sources():
        source = path.read_text(encoding="utf-8")
        assert "shell=True" not in source, f"{path.name}: shell=True found"


def test_no_control_plane_imports():
    for path in _runtime_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert not module.startswith("ai_engineering.control_plane"), (
                    f"{path.name}: control-plane import {module} creates a backchannel"
                )


def test_no_control_plane_mutator_calls():
    for path in _runtime_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in _CONTROL_PLANE_MUTATORS, (
                    f"{path.name}: control-plane mutator call {node.func.attr}"
                )


def test_no_datetime_now_in_rendering_paths():
    # Rendering/serialization/fencing paths must never embed wall-clock
    # time; timestamps live inside evidence records created at execution
    # time. Only the facade's terminal-event timestamp may read the clock.
    for path in _runtime_sources():
        if path.name == "agent_runtime.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("now", "utcnow")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "datetime"
            ):
                raise AssertionError(f"{path.name}: wall-clock call datetime.{node.func.attr}")


def test_facade_clock_use_is_limited_to_event_timestamps():
    source = (RUNTIME_PACKAGE / "agent_runtime.py").read_text(encoding="utf-8")
    assert source.count("datetime.now(") <= 1
    # The clock call must live inside the terminal-event recorder only.
    segment = source.split("def _record_terminal_run_event")[1].split("def ")[0]
    assert "datetime.now(" in segment


def test_no_uuid_or_random():
    for path in _runtime_sources():
        source = path.read_text(encoding="utf-8")
        assert "uuid4" not in source
        assert "import random" not in source
        assert "secrets.token" not in source


class TestAuthoritativeStateImmutability:
    def test_lease_and_workspace_unchanged_by_execution(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        lease_before = fx.workspace_manager.get_lease(WORKSPACE_ID)
        workspace_before = fx.workspace_manager.get_workspace(WORKSPACE_ID)
        request, evidence = execute_default(fx, execution_id="exec-ro1")
        lease_after = fx.workspace_manager.get_lease(WORKSPACE_ID)
        workspace_after = fx.workspace_manager.get_workspace(WORKSPACE_ID)
        assert lease_after == lease_before
        assert workspace_after == workspace_before

    def test_control_plane_cycle_never_touched(self, tmp_path):
        from tests.ai_engineering.control_plane_fixture_helpers import (
            make_lineage,
            make_orchestrator,
        )

        fx = make_local_fixture(tmp_path)
        orch = make_orchestrator(
            task_id=fx.intent.task_id,
            node_id="node-1",
            intent=fx.intent,
            lineage=make_lineage(task_node_id="node-1", target_node_id=None),
        )
        phase_before = orch.state.phase
        blockers_before = orch.state.blockers
        request, evidence = execute_default(fx, execution_id="exec-ro2")
        assert orch.state.phase == phase_before
        assert orch.state.blockers == blockers_before

    def test_evidence_is_frozen(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request, evidence = execute_default(fx, execution_id="exec-ro3")
        with pytest.raises(Exception):
            evidence.exit_code = 99  # type: ignore[misc]

    def test_request_is_frozen_after_execution(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request, evidence = execute_default(fx, execution_id="exec-ro4")
        with pytest.raises(Exception):
            request.command_argv = ("tampered",)  # type: ignore[misc]

    def test_run_record_lifecycle_only_via_canonical_path(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request, evidence = execute_default(fx, execution_id="exec-ro5")
        record = fx.run_registry.get_run(RUN_ID)
        assert record is not None
        # The run reached a terminal state only through the canonical
        # registry transition (event path), never through direct writes.
        assert record.state.value in ("EXITED", "FAILED")
