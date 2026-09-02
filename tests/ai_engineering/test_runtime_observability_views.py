"""PR-13 runtime observability view tests (additive to PR-12 plane)."""

from __future__ import annotations

import json

from ai_engineering.observability.contracts import ProjectionStatus
from ai_engineering.observability.runtime_views import build_runtime_views
from ai_engineering.runtime.runtime_contracts import RuntimeBlockingReason
from tests.ai_engineering.runtime_fixture_helpers import make_local_fixture, make_request
from tests.ai_engineering.test_runtime_idempotency_concurrency import _evidence_for


class TestRuntimeViews:
    def test_empty_projection_complete(self):
        projection = build_runtime_views([])
        assert projection.projection_status == ProjectionStatus.COMPLETE
        assert projection.processes == ()
        assert projection.schema_version == 1

    def test_deterministic_ordering_regardless_of_input_order(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidences = []
        for i in (3, 1, 2):
            request = make_request(fx, execution_id=f"exec-{i}")
            evidences.append(_evidence_for(request))
        a = build_runtime_views(list(reversed(evidences)))
        b = build_runtime_views(evidences)
        assert a.to_dict() == b.to_dict()

    def test_secret_shaped_stdout_redacted(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = _evidence_for(
            make_request(fx),
            stdout="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret.payload",
        )
        projection = build_runtime_views([evidence])
        serialized = json.dumps(projection.to_dict())
        assert "eyJhbGciOiJIUzI1NiJ9" not in serialized
        assert "REDACTED" in serialized

    def test_truncation_disclosed(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidences = [_evidence_for(make_request(fx, execution_id=f"exec-{i}")) for i in range(5)]
        projection = build_runtime_views(evidences, max_processes=2)
        assert projection.processes_truncated is True
        assert projection.process_original_count == 5
        assert projection.process_returned_count == 2
        assert projection.projection_status == ProjectionStatus.PARTIAL
        payload = projection.to_dict()
        assert payload["truncation"]["processes_truncated"] is True

    def test_unverifiable_blocker_propagates(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = _evidence_for(
            make_request(fx),
            state="UNVERIFIABLE",
            exit_code=None,
            exit_proven=False,
            blockers=(RuntimeBlockingReason.RUNTIME_PROCESS_UNVERIFIABLE.value,),
        )
        projection = build_runtime_views([evidence])
        assert projection.projection_status == ProjectionStatus.UNVERIFIABLE

    def test_blockers_surface_in_view(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = _evidence_for(
            make_request(fx),
            blockers=(RuntimeBlockingReason.RUNTIME_OUTPUT_CAPTURE_FAILED.value,),
        )
        projection = build_runtime_views([evidence])
        assert projection.processes[0].blockers == (
            RuntimeBlockingReason.RUNTIME_OUTPUT_CAPTURE_FAILED.value,
        )
        assert projection.projection_status == ProjectionStatus.PARTIAL

    def test_view_is_serializable_json(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = _evidence_for(make_request(fx))
        projection = build_runtime_views([evidence])
        payload = json.loads(json.dumps(projection.to_dict()))
        assert payload["schema_version"] == 1
        assert payload["processes"][0]["run_id"] == "run-1"

    def test_deterministic_serialization_bytes(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = _evidence_for(make_request(fx))
        p1 = build_runtime_views([evidence])
        p2 = build_runtime_views([evidence])
        assert json.dumps(p1.to_dict(), sort_keys=True) == json.dumps(p2.to_dict(), sort_keys=True)

    def test_no_prompt_fields_in_view(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = _evidence_for(make_request(fx), stdout="prompt text")
        projection = build_runtime_views([evidence])
        keys = json.dumps(projection.to_dict())
        assert '"raw_prompt"' not in keys
        assert '"prompt"' not in keys
        assert '"system_prompt"' not in keys
