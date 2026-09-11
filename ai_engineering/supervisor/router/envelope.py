"""Envelope schema and integrity."""

from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

def payload_digest(payload: Mapping[str, Any]) -> str:
    """Compute sha256 digest of canonically serialized payload."""
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

@dataclass(frozen=True, slots=True)
class AgentEnvelope:
    message_id: str
    correlation_id: str
    target_agent: str
    reply_to: str
    payload: Mapping[str, Any]
    payload_digest: str
    timestamp_utc: str
    
    @classmethod
    def create(cls, message_id: str, correlation_id: str, target_agent: str, reply_to: str, payload: Mapping[str, Any], timestamp_utc: str) -> AgentEnvelope:
        digest = payload_digest(payload)
        return cls(
            message_id=message_id,
            correlation_id=correlation_id,
            target_agent=target_agent,
            reply_to=reply_to,
            payload=payload,
            payload_digest=digest,
            timestamp_utc=timestamp_utc
        )
    
    def verify_integrity(self) -> bool:
        return self.payload_digest == payload_digest(self.payload)
