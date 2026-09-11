import json
import pytest
from pathlib import Path
from ai_engineering.supervisor.events import (
    SupervisorEventType,
    create_event,
    canonical_serialize_event,
    deserialize_event
)
from ai_engineering.supervisor.state import SupervisorError

def test_event_payload_tamper_detected():
    payload = {"policy_receipt": {"verdict": "ALLOW", "receipt_id": "receipt123"}}
    ev = create_event(
        run_id="run-1",
        sequence=1,
        previous_event_digest=None,
        event_type=SupervisorEventType.POLICY_EVALUATED,
        state_revision=1,
        task_id="task-1",
        attempt_id="att-1",
        intent_digest="a"*64,
        payload=payload,
        created_at_utc="2026-09-11T00:00:00Z"
    )
    raw = canonical_serialize_event(ev)
    
    # Tamper with the payload inside the serialized string
    raw_tampered = raw.replace('"ALLOW"', '"DENY"')
    
    # deserialize_event should detect the tamper
    with pytest.raises(SupervisorError) as exc:
        deserialize_event(raw_tampered)
    
    assert exc.value.code == "EVENT_PAYLOAD_TAMPERED"

def test_legacy_event_without_payload_replays():
    payload = {"policy_receipt": {"verdict": "ALLOW", "receipt_id": "receipt123"}}
    ev = create_event(
        run_id="run-1",
        sequence=1,
        previous_event_digest=None,
        event_type=SupervisorEventType.POLICY_EVALUATED,
        state_revision=1,
        task_id="task-1",
        attempt_id="att-1",
        intent_digest="a"*64,
        payload=payload,
        created_at_utc="2026-09-11T00:00:00Z"
    )
    raw = canonical_serialize_event(ev)
    
    # Remove the payload field to simulate legacy event
    d = json.loads(raw)
    del d["payload"]
    raw_legacy = json.dumps(d)
    
    # deserialize_event should accept it and set payload to None
    ev_legacy = deserialize_event(raw_legacy)
    assert ev_legacy.payload is None
    assert ev_legacy.payload_digest == ev.payload_digest
