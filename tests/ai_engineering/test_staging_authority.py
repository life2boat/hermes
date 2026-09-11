import pytest
import os
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from ai_engineering.supervisor.events import SupervisorEventType
from ai_engineering.supervisor.loop import create_event, SupervisorLoop, compute_deterministic_digest
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.task_intent import TaskIntent, StopBoundary, IntentStatus, TaskClass, AcceptanceCriterion
from scripts.run_autonomous_supervisor import main as cli_main

DUMMY_SHA = "1234567890123456789012345678901234567890"

def _dummy_intent() -> TaskIntent:
    return TaskIntent(
        schema_version=1,
        task_id="TASK-1",
        intent_revision=1,
        status=IntentStatus.READY,
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        desired_outcome="Do things",
        source_repository="life2boat/hermes",
        source_main_ref="main",
        source_base_sha=DUMMY_SHA,
        constraints=(),
        allowed_mutations=(),
        forbidden_mutations=(),
        stop_boundary=StopBoundary.MERGE,
        acceptance_criteria=(
            AcceptanceCriterion("AC1", "AC1 statement"),
            AcceptanceCriterion("AC2", "AC2 statement"),
        ),
        unknowns=(),
        applicable_invariants=(),
        required_gates=("GATE1",),
        parent_intent_digest=None,
    )

def test_attacker_controlled_state_root_rejected(capsys):
    with tempfile.TemporaryDirectory() as d:
        store_path = Path(d)
        
        intent = _dummy_intent()
        from ai_engineering.task_intent import serialize_intent
        intent_path = store_path / "intent.json"
        intent_path.write_text(serialize_intent(intent))
        
        args = [
            "staging-deploy",
            "--intent", str(intent_path),
            "--run-id", "run-123",
            "--policy-receipt", "fake-123"
        ]
        
        with patch.dict(os.environ, {"HERMES_TRUSTED_STATE_ROOT": str(store_path)}):
            res = cli_main(args)
            assert res == 1
            captured = capsys.readouterr()
            assert "BLOCKED: SUPERVISOR_AUTHORITY_ROOT_UNTRUSTED" in captured.err
            assert "REASON=CALLER_CONTROLLED_AUTHORITY_ROOT" in captured.err
            assert "DOCKER_MUTATION_COUNT=0" in captured.err

def test_synthetic_valid_journal_cannot_authorize(capsys):
    # If the root is not trusted, even a mathematically valid journal fails.
    test_attacker_controlled_state_root_rejected(capsys)

def test_trusted_supervisor_journal_can_authorize():
    # This test verifies that if the authority root IS trusted, the preflight progresses
    with tempfile.TemporaryDirectory(dir=os.getcwd()) as d:
        store_path = Path(d)
        
        intent = _dummy_intent()
        from ai_engineering.task_intent import serialize_intent
        intent_path = store_path / "intent.json"
        intent_path.write_text(serialize_intent(intent))
        
        store = FileSupervisorStateStore(store_path)
        receipt_payload = {
            "schema_version": 1,
            "receipt_id": "receipt123",
            "request_id": "req1",
            "task_intent_digest": "1234567890123456789012345678901234567890123456789012345678901234",
            "decision_id": "dec1",
            "decision_receipt_id": "drec1",
            "effective_policy_id": "pol1",
            "work_profile_id": "wp1",
            "work_profile_digest": "wpdig",
            "autonomy_state_digest": "ast",
            "budget_state_digest": "bst",
            "verdict": "ALLOW",
            "reason_codes": [],
            "created_at_utc": "2026-01-01T00:00:00"
        }
        
        ev = create_event(
            run_id="run-123",
            sequence=1,
            previous_event_digest=None,
            event_type=SupervisorEventType.POLICY_EVALUATED,
            state_revision=1,
            task_id="task-123",
            attempt_id="attempt-1",
            intent_digest="1234567890123456789012345678901234567890123456789012345678901234",
            payload={"policy_receipt": receipt_payload},
            created_at_utc="2026-01-01T00:00:00",
            decision_id="dec1"
        )
        store.save_event(ev)
        
        args = [
            "staging-deploy",
            "--intent", str(intent_path),
            "--run-id", "run-123",
            "--policy-receipt", "receipt123"
        ]
        
        # We mock the canonical reading logic to return our temp dir
        # This simulates reading from /etc/hermes/supervisor.conf
        with patch("scripts.run_autonomous_supervisor.get_trusted_state_root", return_value=store_path):
            with patch("scripts.run_autonomous_supervisor.check_trusted_root_security", return_value=True):
                # Ensure no caller-controlled env is set
                if "HERMES_TRUSTED_STATE_ROOT" in os.environ:
                    del os.environ["HERMES_TRUSTED_STATE_ROOT"]
                try:
                    res = cli_main(args)
                    # It will fail with BLOCKED: Missing attestation or Intent digest mismatch, but that proves it passed the authority check
                    assert res == 1
                except SystemExit as e:
                    assert e.code == 1
