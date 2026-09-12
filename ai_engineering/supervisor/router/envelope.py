from __future__ import annotations
import json
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping
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

def compute_payload_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(canonical).hexdigest()

@dataclass(frozen=True, slots=True)
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
    payload: dict[str, Any]
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
        return compute_payload_digest(self.payload) == self.payload_digest

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "message_id": self.message_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "sender_agent": self.sender_agent,
            "recipient_agent": self.recipient_agent,
            "recipient_capability": self.recipient_capability,
            "message_type": self.message_type.value if hasattr(self.message_type, "value") else str(self.message_type),
            "payload": self.payload,
            "payload_digest": self.payload_digest,
            "task_intent_id": self.task_intent_id,
            "task_intent_digest": self.task_intent_digest,
            "policy_receipt_id": self.policy_receipt_id,
            "policy_receipt_digest": self.policy_receipt_digest,
            "effect_class": self.effect_class.value if hasattr(self.effect_class, "value") else str(self.effect_class),
            "stop_boundary": self.stop_boundary.value if hasattr(self.stop_boundary, "value") else str(self.stop_boundary),
            "created_at_utc": self.created_at_utc,
            "expires_at_utc": self.expires_at_utc,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> AgentEnvelope:
        raw_msg_type = d["message_type"]
        if isinstance(raw_msg_type, MessageType):
            msg_type = raw_msg_type
        elif isinstance(raw_msg_type, str):
            try:
                msg_type = MessageType(raw_msg_type)
            except ValueError as exc:
                raise ValueError(f"AGENT_MESSAGE_TYPE_UNSUPPORTED: {raw_msg_type}") from exc
        else:
            raise ValueError(f"AGENT_MESSAGE_TYPE_UNSUPPORTED: {raw_msg_type}")

        raw_ec = d["effect_class"]
        effect_class = raw_ec if isinstance(raw_ec, EffectClass) else EffectClass(raw_ec)

        raw_sb = d["stop_boundary"]
        stop_boundary = raw_sb if isinstance(raw_sb, StopBoundary) else StopBoundary(raw_sb)

        return cls(
            schema_version=d.get("schema_version", "hermes.agent-envelope.v1"),
            message_id=d["message_id"],
            correlation_id=d["correlation_id"],
            causation_id=d["causation_id"],
            run_id=d["run_id"],
            task_id=d["task_id"],
            attempt_id=d["attempt_id"],
            sender_agent=d["sender_agent"],
            recipient_agent=d["recipient_agent"],
            recipient_capability=d["recipient_capability"],
            message_type=msg_type,
            payload=dict(d["payload"]),
            payload_digest=d["payload_digest"],
            task_intent_id=d["task_intent_id"],
            task_intent_digest=d["task_intent_digest"],
            policy_receipt_id=d["policy_receipt_id"],
            policy_receipt_digest=d["policy_receipt_digest"],
            effect_class=effect_class,
            stop_boundary=stop_boundary,
            created_at_utc=d.get("created_at_utc") or d.get("created_at") or "",
            expires_at_utc=d.get("expires_at_utc", ""),
            retry_count=d.get("retry_count", 0),
            max_retries=d.get("max_retries", 3),
        )
