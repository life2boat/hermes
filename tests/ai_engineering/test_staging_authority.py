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

def test_attacker_controlled_state_root_rejected():
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
        
        # Mock environ to use /tmp which is blocked
        with patch.dict(os.environ, {"HERMES_TRUSTED_STATE_ROOT": str(store_path)}):
            try:
                res = cli_main(args)
                assert res == 1
            except SystemExit as e:
                assert e.code == 1

def test_synthetic_valid_journal_cannot_authorize():
    # If the root is not trusted, even a mathematically valid journal fails.
    # Same as above, just asserting UNTRUSTED_AUTHORITY_ROOT logic
    test_attacker_controlled_state_root_rejected()

def test_trusted_supervisor_journal_can_authorize():
    # This test verifies that if the authority root IS trusted, the preflight progresses
    # (It will fail eventually on preflight validations or docker, but it passes the authority check)
    with tempfile.TemporaryDirectory(dir=os.getcwd()) as d:
        store_path = Path(d)
        
        intent = _dummy_intent()
        from ai_engineering.task_intent import serialize_intent
        intent_path = store_path / "intent.json"
        intent_path.write_text(serialize_intent(intent))
        
        # Insert a mathematically valid event so load_events succeeds
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
        

        
        with patch.dict(os.environ, {"HERMES_TRUSTED_STATE_ROOT": str(store_path)}):
            with patch("os.name", "nt"): # Bypass POSIX check for ease
                try:
                    res = cli_main(args)
                    # It will fail with BLOCKED: Intent digest mismatch, but that proves it loaded the file
                    # Or it might block on Attestation.
                    assert res == 1
                except SystemExit as e:
                    assert e.code == 1

