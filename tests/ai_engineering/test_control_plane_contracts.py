"""Unit tests for control plane contracts and serialization (PR-11.1)."""

from __future__ import annotations

import pytest

from ai_engineering.control_plane.barriers import ProductionSerializationBarrier
from ai_engineering.control_plane.contracts import (
    CONTROL_PLANE_CONTRACT_VERSION,
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
    ControlPlanePhase,
    ValidationEvidence,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.handoff import NodeHandoff
from tests.ai_engineering.control_plane_fixture_helpers import SHA, make_validation_evidence


def test_control_plane_phase_and_events():
    assert ControlPlanePhase.CREATED == "CREATED"
    assert ControlPlaneEventType.WORKSPACE_READY == "WORKSPACE_READY"


def test_control_plane_contract_version_bumped():
    assert CONTROL_PLANE_CONTRACT_VERSION == "4.1.1"


def test_cycle_state_serialization():
    from tests.ai_engineering.control_plane_fixture_helpers import make_state

    st = make_state()
    d = st.to_dict()
    assert d["cycle_id"] == "c1"
    assert d["phase"] == "CREATED"
    assert d["execution_epoch"] == 1

    restored = EngineeringCycleState.from_dict(d)
    assert restored == st


# ---------------------------------------------------------------------------
# D2: NodeHandoff evidence-ref path safety (all audited bypass shapes)
# ---------------------------------------------------------------------------

_AUDITED_PATHS = [
    "/tmp/foreign/file",
    "C:\\foreign\\file",
    "C:/foreign/worktree/file.json",
    "C:\\foreign\\worktree\\file.json",
    "\\\\server\\share\\file",
    "//server/share/file",
    "../foreign/file",
    "nested/../../foreign/file",
    "./../foreign/file",
]


@pytest.mark.parametrize("ref", _AUDITED_PATHS)
def test_handoff_rejects_all_audited_foreign_paths(ref):
    with pytest.raises(ControlPlaneError) as exc:
        NodeHandoff(
            handoff_id="h1",
            task_id="t1",
            source_node_id="n1",
            target_node_id="n2",
            cycle_id="c1",
            base_sha=SHA,
            execution_epoch=1,
            evidence_refs=(ref,),
        )
    assert exc.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value


@pytest.mark.parametrize(
    "ref", ["snap-1", "diff-01", "val-ref_2", "src/app.py", "docs/HERMES_INVARIANTS.md"]
)
def test_handoff_accepts_evidence_ids_and_relative_paths(ref):
    handoff = NodeHandoff(
        handoff_id="h1",
        task_id="t1",
        source_node_id="n1",
        target_node_id="n2",
        cycle_id="c1",
        base_sha=SHA,
        execution_epoch=1,
        evidence_refs=(ref,),
    )
    assert handoff.evidence_refs == (ref,)


def test_handoff_contract_roundtrip():
    handoff = NodeHandoff(
        handoff_id="h-01",
        task_id="t-01",
        source_node_id="n-src",
        target_node_id="n-tgt",
        cycle_id="c-01",
        base_sha=SHA,
        execution_epoch=1,
        evidence_refs=("snapshot-01", "diff-01"),
    )
    assert handoff.to_dict()["evidence_refs"] == ["snapshot-01", "diff-01"]


# ---------------------------------------------------------------------------
# ValidationEvidence contract
# ---------------------------------------------------------------------------


def test_validation_evidence_valid():
    evidence = make_validation_evidence()
    assert evidence.candidate_id == "cand-1"
    assert evidence.evidence_refs == ("snap-1",)


def test_validation_evidence_requires_concrete_refs():
    with pytest.raises(ControlPlaneError) as exc:
        make_validation_evidence(evidence_refs=())
    assert exc.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value


def test_validation_evidence_rejects_foreign_paths():
    with pytest.raises(ControlPlaneError):
        make_validation_evidence(evidence_refs=("C:/foreign/file.json",))


def test_validation_evidence_rejects_bad_sha():
    with pytest.raises(ControlPlaneError):
        make_validation_evidence(base_sha="nothex")


def test_barrier_states():
    barrier_not_ready = ProductionSerializationBarrier(
        active_mutation_agents=1,
        single_production_owner="agent-01",
    )
    assert barrier_not_ready.ready is False

    barrier_ready = ProductionSerializationBarrier(
        active_mutation_agents=0,
        single_production_owner="agent-01",
    )
    assert barrier_ready.ready is True
