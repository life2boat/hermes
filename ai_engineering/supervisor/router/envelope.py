from __future__ import annotations
import json
import hashlib
from dataclasses import dataclass, field
from typing import Optional, Any, Dict
from enum import Enum
from ai_engineering.contracts import EffectClass, StopBoundary

class MessageType(str, Enum):
    WORK_REQUEST = "WORK_REQUEST"
    WORK_PROGRESS = "WORK_PROGRESS"
    WORK_RESULT = "WORK_RESULT"
    WORK_FAILURE = "WORK_FAILURE"
    VERIFICATION_REQUEST = "VERIFICATION_REQUEST"
    VERIFICATION_RESULT = "VERIFICATION_RESULT"
    CANCEL_REQUEST = "CANCEL_REQUEST"
    CANCEL_ACK = "CANCEL_ACK"

@dataclass(frozen=True)
class AgentEnvelope:
    message_id: str
    correlation_id: str
    causation_id: str
    run_id: str
    task_id: str
    attempt_id: str
    sender_agent: str
    recipient_agent: str
    recipient_capability: str
    message_type: MessageType
    payload: Dict[str, Any]
    payload_digest: str
    task_intent_id: str
    task_intent_digest: str
    policy_receipt_id: str
    policy_receipt_digest: str
    effect_class: EffectClass
    stop_boundary: StopBoundary
    created_at_utc: str
    expires_at_utc: str
    retry_count: int = 0
    max_retries: int = 3
    schema_version: str = "hermes.agent-envelope.v1"

    def verify_integrity(self) -> bool:
        canonical = json.dumps(self.payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(canonical).hexdigest() == self.payload_digest
