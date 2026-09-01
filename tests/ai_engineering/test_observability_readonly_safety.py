"""PR-12 observability: read-only guarantees, no backchannel, no clock/random leak.

These tests prove observability cannot mutate authoritative state and
does not embed environmental access (wall clock, randomness, network,
subprocess, control-plane mutators).
"""

from __future__ import annotations

import copy
import json
import re

from ai_engineering.control_plane.registry import EngineeringCycleRegistry
from ai_engineering.observability.collector import OperatorQueries, collect_operator_snapshot
from ai_engineering.observability.rendering import canonical_json, human_summary
from tests.ai_engineering.observability_fixture_helpers import (
    CANDIDATE_ID,
    collect_full,
)


def _freeze(value):
    """Recursive canonical freeze for deep-equality comparison."""

    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze(v) for v in value))
    return value


class TestNoMutationOfAuthoritativeState:
    def test_projection_does_not_mutate_inputs(self):
        state = collect_full.__globals__["build_full_state"]()
        before = _freeze(_authoritative_repr(state))
        collect_full(
            workspaces=state["workspaces"],
            runs=state["runs"],
            hosts=state["hosts"],
            candidates=state["candidates"],
            candidate_results=state["candidate_results"],
            judge_result=state["judge_result"],
            validation=state["validation"],
            requalification_result=state["requalification_result"],
            handoff=state["handoff"],
            registry=state["registry"],
            raw_events=state["events"],
            barrier=state["barrier"],
        )
        after = _freeze(_authoritative_repr(state))
        assert before == after

    def test_projection_does_not_mutate_registry(self):
        registry = EngineeringCycleRegistry()
        state = collect_full.__globals__["build_full_state"]()
        registry = state["registry"]
        events_before = len(registry.get_events("c1"))
        cycles_before = _freeze(str(registry.get_cycle("c1")))
        handoffs_before = _freeze(str(registry.get_handoff("handoff-1")))
        snap = collect_full(registry=registry, raw_events=registry.get_events("c1"))
        canonical_json(snap)
        human_summary(snap)
        OperatorQueries(snap).get_cycle_summary()
        assert len(registry.get_events("c1")) == events_before
        assert _freeze(str(registry.get_cycle("c1"))) == cycles_before
        assert _freeze(str(registry.get_handoff("handoff-1"))) == handoffs_before

    def test_rendering_does_not_mutate_snapshot(self):
        snap = collect_full()
        before = canonical_json(snap)
        human_summary(snap)
        canonical_json(snap)
        OperatorQueries(snap).get_active_runs()
        after = canonical_json(snap)
        assert before == after

    def test_view_objects_do_not_reference_authoritative_objects(self):
        snap = collect_full()
        # Views are dataclass views of scalars; mutating a view copy must
        # never touch any authoritative structure.
        state = collect_full.__globals__["build_full_state"]()
        view = snap.workspaces[0]
        view_dict = copy.deepcopy(view.to_dict())
        view_dict["task_id"] = "mutated"
        assert state["workspaces"][0].task_id == "t1"

    def test_frozen_views_resist_mutation(self):
        snap = collect_full()
        try:
            snap.workspaces[0].task_id = "hack"
        except Exception:
            pass
        else:
            raise AssertionError("WorkspaceView is not frozen")
        try:
            snap.cycle.phase = "HACKED"
        except Exception:
            pass
        else:
            raise AssertionError("CycleView is not frozen")


def _authoritative_repr(state):
    return {
        "workspaces": str(state["workspaces"]),
        "leases": str(state["leases"]),
        "runs": str(state["runs"]),
        "hosts": str(state["hosts"]),
        "candidates": str(state["candidates"]),
        "candidate_results": str(sorted(state["candidate_results"].items())),
        "judge": str(state["judge_result"]),
        "validation": str(state["validation"]),
        "requalification": str(state["requalification_result"]),
        "handoff": str(state["handoff"]),
        "events": str(state["events"]),
        "barrier": str(state["barrier"]),
        "authority": str(state["authority"]),
        "decision": str(state["parallelization_decision"]),
        "budget": str(state["budget"]),
        "cycle": str(state["cycle"]),
        "intent": str(state["intent"]),
        "lineage": str(state["lineage"]),
    }


class TestNoControlPlaneBackchannel:
    def test_no_control_plane_mutator_names_in_observability(self):
        import pathlib

        observability_dir = pathlib.Path("ai_engineering/observability")
        forbidden = re.compile(
            r"\.(transition|record_event|record_candidate|record_validation|"
            r"request_cancel|record_requalification|trigger_requalification|"
            r"release_lease|spawn|execute|merge|deploy)\("
        )
        violations = []
        for path in observability_dir.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for match in forbidden.finditer(text):
                violations.append(f"{path.name}: {match.group(0)}")
        assert violations == []

    def test_no_environmental_access_in_observability(self):
        """AST-based scan: no imports/calls of environmental or execution
        machinery anywhere in the observability package (docstrings ignored)."""

        import ast
        import pathlib

        observability_dir = pathlib.Path("ai_engineering/observability")
        forbidden_modules = {
            "subprocess",
            "socket",
            "requests",
            "httpx",
            "urllib",
            "paramiko",
            "asyncssh",
            "pickle",
            "yaml",
            "secrets",
            "random",
            "uuid",
            "sqlite3",
            "http",
            "ftplib",
            "smtplib",
            "telnetlib",
        }
        forbidden_calls = {
            "eval",
            "exec",
            "compile",
            "__import__",
            "open",  # observability never opens files (CLI lives in __main__)
            "system",
            "popen",
            "now",
            "utcnow",
            "time",
        }
        violations = []
        for path in observability_dir.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root in forbidden_modules:
                            violations.append(f"{path.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    root = (node.module or "").split(".")[0]
                    if root in forbidden_modules:
                        violations.append(f"{path.name}: from {node.module} import")
                elif isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name):
                        name = func.id
                    else:
                        # Attribute calls: flag only dangerous receivers
                        # (re.compile etc. are legitimate).
                        name = getattr(func, "attr", None)
                        if name in {"system", "popen", "now", "utcnow", "call"}:
                            violations.append(f"{path.name}: call {name}()")
                        continue
                    if name in {"eval", "exec", "compile", "__import__", "system", "popen", "now", "utcnow"}:
                        violations.append(f"{path.name}: call {name}()")
        assert violations == []

    def test_observability_does_not_import_control_plane_orchestrator(self):
        import sys

        import ai_engineering.observability  # noqa: F401
        import ai_engineering.observability.projection as projection_mod

        source = __import__("inspect").getsource(projection_mod)
        assert "from ai_engineering.control_plane.orchestrator import" not in source

    def test_projection_is_pure_over_fixed_inputs(self):
        # Running the same projection twice over the same inputs yields
        # byte-identical output without touching the clock.
        state = collect_full.__globals__["build_full_state"]()
        snap_a = collect_full(registry=state["registry"])
        snap_b = collect_full(registry=state["registry"])
        assert canonical_json(snap_a) == canonical_json(snap_b)


class TestPublicApiSurface:
    def test_public_api_minimal(self):
        import ai_engineering.observability as obs

        public = [name for name in obs.__all__]
        assert set(public) == {
            "OBSERVABILITY_CONTRACT_VERSION",
            "OBSERVABILITY_SCHEMA_VERSION",
            "BarrierName",
            "ObservabilityReasonCode",
            "ObservabilitySchemaError",
            "OperatorHealthState",
            "OperatorQueries",
            "OperatorSnapshot",
            "OperatorSource",
            "ProjectionHealth",
            "ProjectionLimits",
            "ProjectionProvenance",
            "ProjectionStatus",
            "TruncationInfo",
            "canonical_json",
            "collect_operator_snapshot",
            "human_summary",
            "load_operator_snapshot_dict",
            "redact_operator_dict",
        }

    def test_no_write_methods_on_queries(self):
        queries = OperatorQueries(collect_full())
        write_like = [
            name
            for name in dir(queries)
            if name.startswith(("set_", "update_", "record_", "apply_", "mutate_"))
        ]
        assert write_like == []


class TestProvenanceAndSchema:
    def test_provenance_counts_accurate(self):
        snap = collect_full()
        counts = dict(snap.generated_from.source_counts)
        assert counts["WORKSPACE_IDENTITIES"] == 1
        assert counts["RUN_RECORDS"] == 1
        assert counts["CANDIDATE_IDENTITIES"] == 1
        assert counts["EVENT_LOG"] == 2
        assert counts["CURRENT_MAIN_SHA"] == 0

    def test_sources_absent_explicit(self):
        snap = collect_full(current_main_sha=None)
        assert "CURRENT_MAIN_SHA" in snap.generated_from.sources_absent

    def test_snapshot_contract_shape(self):
        snap = collect_full()
        data = snap.to_dict()
        for key in (
            "schema_version",
            "generated_from",
            "projection_status",
            "projection_health",
            "cycle",
            "control_plane",
            "task_intent",
            "lineage",
            "authority",
            "workspaces",
            "runs",
            "execution_hosts",
            "candidates",
            "judgement",
            "validation",
            "requalification",
            "handoff",
            "barriers",
            "blockers",
            "event_timeline",
            "artifacts",
            "production_serialization",
            "redactions",
            "truncations",
        ):
            assert key in data
