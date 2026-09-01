"""PR-12 observability: deterministic serialization, bounded output, scale."""

from __future__ import annotations

import json

from ai_engineering.observability.contracts import ProjectionLimits, ProjectionStatus
from ai_engineering.observability.rendering import canonical_json, human_summary
from tests.ai_engineering.observability_fixture_helpers import (
    collect_full,
    make_event,
    make_candidate,
    make_workspace,
)


class TestDeterministicSerialization:
    def test_same_state_same_json_bytes(self):
        raw_a = canonical_json(collect_full())
        raw_b = canonical_json(collect_full())
        assert raw_a == raw_b

    def test_insertion_order_does_not_change_output(self):
        events_a = [
            make_event(event_id="evt-1", created_at="2026-01-15T12:05:00+00:00"),
            make_event(event_id="evt-2", created_at="2026-01-15T12:06:00+00:00"),
            make_event(event_id="evt-3", created_at="2026-01-15T12:07:00+00:00"),
        ]
        events_b = [events_a[2], events_a[0], events_a[1]]
        raw_a = canonical_json(collect_full(raw_events=events_a))
        raw_b = canonical_json(collect_full(raw_events=events_b))
        assert raw_a == raw_b

    def test_key_order_does_not_change_output(self):
        data = collect_full().to_dict()
        reordered = {k: data[k] for k in reversed(list(data.keys()))}
        raw_a = canonical_json(data)
        raw_b = canonical_json(reordered)
        assert raw_a == raw_b

    def test_human_rendering_deterministic(self):
        text_a = human_summary(collect_full())
        text_b = human_summary(collect_full())
        assert text_a == text_b

    def test_canonical_json_is_valid_json_object(self):
        raw = canonical_json(collect_full())
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        assert "schema_version" in parsed

    def test_no_object_repr_leakage(self):
        raw = canonical_json(collect_full())
        assert "0x" not in raw
        assert "<ai_engineering" not in raw
        assert "<object at" not in raw

    def test_no_python_enum_repr(self):
        raw = canonical_json(collect_full())
        assert "ProjectionStatus." not in raw
        assert "ControlPlanePhase." not in raw

    def test_human_rendering_matches_state(self):
        snap = collect_full()
        text = human_summary(snap)
        assert snap.control_plane.phase in text
        assert "health: OK" in text


class TestBoundedOutput:
    def test_bounded_events(self):
        events = [
            make_event(event_id=f"evt-{i:04d}", created_at=f"2026-01-15T12:{i // 60:02d}:{i % 60:02d}+00:00")
            for i in range(120)
        ]
        snap = collect_full(raw_events=events, limits=ProjectionLimits(max_events=25))
        assert len(snap.event_timeline) == 25
        assert any(t.field == "event_timeline" and t.truncated for t in snap.truncations)
        assert snap.projection_status is ProjectionStatus.PARTIAL

    def test_bounded_candidates(self):
        candidates = tuple(
            make_candidate(candidate_id=f"cand-{i:03d}", workspace_id=f"ws-{i:03d}")
            for i in range(80)
        )
        workspaces = tuple(
            make_workspace(workspace_id=f"ws-{i:03d}", candidate_id=f"cand-{i:03d}")
            for i in range(80)
        )
        snap = collect_full(
            candidates=candidates,
            workspaces=workspaces,
            limits=ProjectionLimits(max_candidates=20),
        )
        assert len(snap.candidates) == 20
        assert any(t.field == "candidates" and t.truncated for t in snap.truncations)
        assert snap.truncations[0].original_count == 80

    def test_bounded_workspaces(self):
        workspaces = tuple(
            make_workspace(workspace_id=f"ws-{i:03d}") for i in range(70)
        )
        snap = collect_full(workspaces=workspaces, limits=ProjectionLimits(max_workspaces=10))
        assert len(snap.workspaces) == 10
        assert any(t.field == "workspaces" for t in snap.truncations)

    def test_bounded_validation_evidence_refs(self):
        from ai_engineering.control_plane.contracts import ValidationEvidence

        validation = ValidationEvidence(
            evidence_id="val-big",
            cycle_id="c1",
            task_id="t1",
            node_id="n1",
            candidate_id="cand-1",
            base_sha=collect_full().cycle.source_base_sha,
            execution_epoch=1,
            evidence_refs=tuple(f"ref-{i:03d}" for i in range(50)),
        )
        snap = collect_full(validation=validation, limits=ProjectionLimits(max_evidence_refs=5))
        assert len(snap.validation.evidence_refs) == 5
        assert snap.validation.evidence_refs_count == 50

    def test_truncation_records_are_deterministic(self):
        events = [
            make_event(event_id=f"evt-{i:04d}", created_at=f"2026-01-15T12:{i // 60:02d}:{i % 60:02d}+00:00")
            for i in range(40)
        ]
        raw_a = canonical_json(collect_full(raw_events=events, limits=ProjectionLimits(max_events=5)))
        raw_b = canonical_json(collect_full(raw_events=events, limits=ProjectionLimits(max_events=5)))
        assert raw_a == raw_b

    def test_limits_reject_invalid(self):
        import pytest
        from ai_engineering.observability.contracts import ProjectionLimits as Limits

        with pytest.raises(Exception):
            Limits(max_events=0)
        with pytest.raises(Exception):
            Limits(max_runs=-1)


class TestScale:
    def test_thousand_events_bounded_and_fast_enough(self):
        events = [
            make_event(
                event_id=f"evt-{i:06d}",
                created_at="2026-01-15T12:00:00+00:00",
                workspace_id=f"ws-{i % 50:03d}",
            )
            for i in range(1000)
        ]
        snap = collect_full(raw_events=events, limits=ProjectionLimits(max_events=100))
        assert len(snap.event_timeline) == 100
        assert snap.truncations[0].original_count == 1000
        assert snap.projection_status is ProjectionStatus.PARTIAL

    def test_many_candidates_deterministic_truncation(self):
        candidates = tuple(
            make_candidate(candidate_id=f"cand-{i:04d}", workspace_id=f"ws-{i:04d}")
            for i in range(300)
        )
        workspaces = tuple(
            make_workspace(workspace_id=f"ws-{i:04d}", candidate_id=f"cand-{i:04d}")
            for i in range(300)
        )
        snap_a = collect_full(
            candidates=candidates, workspaces=workspaces, limits=ProjectionLimits(max_candidates=50)
        )
        snap_b = collect_full(
            candidates=candidates, workspaces=workspaces, limits=ProjectionLimits(max_candidates=50)
        )
        assert canonical_json(snap_a) == canonical_json(snap_b)
        assert len(snap_a.candidates) == 50
