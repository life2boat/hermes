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

def test_arbitrary_receipt_file_is_not_authority():
    with tempfile.TemporaryDirectory() as d:
        store_path = Path(d)
        
        intent = _dummy_intent()
        from ai_engineering.task_intent import serialize_intent
        intent_path = store_path / "intent.json"
        intent_path.write_text(serialize_intent(intent))
        
        args = [
            "staging-deploy",
            "--intent", str(intent_path),
            "--state-dir", str(store_path),
            "--run-id", "run-123",
            "--policy-receipt", "fake-123"
        ]
        
        with patch("sys.stderr"):
            try:
                res = cli_main(args)
                assert res == 1
            except SystemExit as e:
                assert e.code == 1

def test_tampered_receipt_blocked():
    with tempfile.TemporaryDirectory() as d:
        store_path = Path(d)
        store = FileSupervisorStateStore(store_path)
        
        intent = _dummy_intent()
        from ai_engineering.task_intent import serialize_intent
        intent_path = store_path / "intent.json"
        intent_path.write_text(serialize_intent(intent))
        
        # Insert event into journal
        from ai_engineering.supervisor.events import create_event
        receipt_payload = {
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
        
        # Tampered
        receipt_payload["receipt_id"] = "tampered-id"
        
        ev = create_event(
            run_id="run-123",
            sequence=1,
            previous_event_digest=None,
            event_type=SupervisorEventType.POLICY_EVALUATED,
            state_revision=1,
            task_id="task-123",
            attempt_id="attempt-1",
            intent_digest="1234567890123456789012345678901234567890123456789012345678901234",
            payload={"receipt_id": "tampered-id", "policy_receipt": receipt_payload},
            created_at_utc="2026-01-01T00:00:00",
            decision_id="dec1"
        )
        store.save_event(ev)
        
        args = [
            "staging-deploy",
            "--intent", str(intent_path),
            "--state-dir", str(store_path),
            "--run-id", "run-123",
            "--policy-receipt", "tampered-id"
        ]
        
        with patch("sys.stderr"):
            try:
                res = cli_main(args)
                assert res == 1
            except SystemExit as e:
                assert e.code == 1

def test_receipt_not_in_journal_blocked():
    with tempfile.TemporaryDirectory() as d:
        store_path = Path(d)
        store = FileSupervisorStateStore(store_path)
        
        intent = _dummy_intent()
        from ai_engineering.task_intent import serialize_intent
        intent_path = store_path / "intent.json"
        intent_path.write_text(serialize_intent(intent))
        
        args = [
            "staging-deploy",
            "--intent", str(intent_path),
            "--state-dir", str(store_path),
            "--run-id", "run-123",
            "--policy-receipt", "not-in-journal"
        ]
        
        with patch("sys.stderr"):
            try:
                res = cli_main(args)
                assert res == 1
            except SystemExit as e:
                assert e.code == 1
