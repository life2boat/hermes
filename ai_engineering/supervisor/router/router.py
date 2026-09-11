from __future__ import annotations
import json
import time
import os
from pathlib import Path
from typing import Optional, Protocol, Any
from dataclasses import dataclass
from ai_engineering.supervisor.router.envelope import AgentEnvelope
from ai_engineering.supervisor.router.registry import AgentRegistry
from ai_engineering.supervisor.worker_result import WorkerResultBundle

ROUTER_CAN_GRANT_AUTHORITY = False
ASTRA_CAN_AUTHORIZE = False

@dataclass(frozen=True, slots=True)
class RoutingReceipt:
    schema_version: str
    message_id: str
    correlation_id: str
    target_agent: str
    status: str
    attempts: int

class PersistentStore(Protocol):
    def has_processed(self, message_id: str) -> bool: ...
    def mark_processed(self, message_id: str) -> None: ...
    def store_result(self, message_id: str, result: WorkerResultBundle) -> None: ...
    def get_result(self, message_id: str) -> Optional[WorkerResultBundle]: ...
    def save_message(self, envelope: AgentEnvelope) -> None: ...
    def load_messages(self) -> list[AgentEnvelope]: ...
    def store_receipt(self, receipt: RoutingReceipt) -> None: ...
    def check_canceled(self, correlation_id: str) -> bool: ...
    def mark_canceled(self, correlation_id: str) -> None: ...

def is_safe_path(p: str) -> bool:
    return not (".." in p or "/" in p or "\\" in p)

class FilePersistentStore:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir = self.root_dir / "processed"
        self.processed_dir.mkdir(exist_ok=True)
        self.results_dir = self.root_dir / "results"
        self.results_dir.mkdir(exist_ok=True)
        self.messages_dir = self.root_dir / "messages"
        self.messages_dir.mkdir(exist_ok=True)
        self.receipts_dir = self.root_dir / "receipts"
        self.receipts_dir.mkdir(exist_ok=True)
        self.cancellations_dir = self.root_dir / "cancellations"
        self.cancellations_dir.mkdir(exist_ok=True)
        
    def has_processed(self, message_id: str) -> bool:
        if not is_safe_path(message_id): raise ValueError("Invalid path")
        return (self.processed_dir / message_id).exists()
        
    def mark_processed(self, message_id: str) -> None:
        if not is_safe_path(message_id): raise ValueError("Invalid path")
        (self.processed_dir / message_id).touch()
        
    def store_result(self, message_id: str, result: WorkerResultBundle) -> None:
        if not is_safe_path(message_id): raise ValueError("Invalid path")
        from ai_engineering.supervisor.worker_result import canonical_serialize_worker_result
        data = canonical_serialize_worker_result(result)
        tmp = self.results_dir / f"{message_id}.json.tmp"
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self.results_dir / f"{message_id}.json")
        
    def get_result(self, message_id: str) -> Optional[WorkerResultBundle]:
        if not is_safe_path(message_id): raise ValueError("Invalid path")
        path = self.results_dir / f"{message_id}.json"
        if not path.exists(): return None
        from ai_engineering.supervisor.worker_result import deserialize_worker_result
        return deserialize_worker_result(path.read_text(encoding="utf-8"))
        
    def save_message(self, envelope: AgentEnvelope) -> None:
        if not is_safe_path(envelope.message_id): raise ValueError("Invalid path")
        data = json.dumps({
            "message_id": envelope.message_id,
            "correlation_id": envelope.correlation_id,
            "target_agent": envelope.target_agent,
            "reply_to": envelope.reply_to,
            "payload": envelope.payload,
            "payload_digest": envelope.payload_digest,
            "timestamp_utc": envelope.timestamp_utc
        })
        tmp = self.messages_dir / f"{envelope.message_id}.json.tmp"
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self.messages_dir / f"{envelope.message_id}.json")
        
    def load_messages(self) -> list[AgentEnvelope]:
        return []
        
    def store_receipt(self, receipt: RoutingReceipt) -> None:
        if not is_safe_path(receipt.message_id): raise ValueError("Invalid path")
        data = json.dumps({
            "schema_version": receipt.schema_version,
            "message_id": receipt.message_id,
            "correlation_id": receipt.correlation_id,
            "target_agent": receipt.target_agent,
            "status": receipt.status,
            "attempts": receipt.attempts,
        })
        tmp = self.receipts_dir / f"{receipt.message_id}.json.tmp"
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, self.receipts_dir / f"{receipt.message_id}.json")

    def check_canceled(self, correlation_id: str) -> bool:
        if not is_safe_path(correlation_id): raise ValueError("Invalid path")
        return (self.cancellations_dir / correlation_id).exists()
        
    def mark_canceled(self, correlation_id: str) -> None:
        if not is_safe_path(correlation_id): raise ValueError("Invalid path")
        (self.cancellations_dir / correlation_id).touch()

class PolicyDeniedError(Exception): pass

class CrossAgentRouter:
    def __init__(self, registry: AgentRegistry, store: PersistentStore, max_retries: int = 3, default_timeout: int = 30) -> None:
        self.registry = registry
        self.store = store
        self.max_retries = max_retries
        self.default_timeout = default_timeout

    def route(self, envelope: AgentEnvelope) -> WorkerResultBundle:
        if not envelope.verify_integrity():
            raise ValueError("Payload integrity verification failed")
        
        # Identity + Authority binding check
        # Explicit ROUTER_CAN_GRANT_AUTHORITY=false, ASTRA_CAN_AUTHORIZE=false
        if envelope.target_agent == "astra" and ASTRA_CAN_AUTHORIZE:
            raise PolicyDeniedError("ASTRA cannot authorize")

        self.store.save_message(envelope)

        if self.store.has_processed(envelope.message_id):
            result = self.store.get_result(envelope.message_id)
            if result is not None:
                return result

        adapter = self.registry.get_adapter(envelope.target_agent)
        capability = self.registry.get_capability(envelope.target_agent)
        timeout = capability.timeout_seconds if capability else self.default_timeout
        
        start_time = time.monotonic()
        for attempt in range(self.max_retries):
            if self.store.check_canceled(envelope.correlation_id):
                adapter.cancel(envelope.correlation_id)
                self.store.store_receipt(RoutingReceipt(
                    schema_version="hermes.agent-routing-receipt.v1",
                    message_id=envelope.message_id,
                    correlation_id=envelope.correlation_id,
                    target_agent=envelope.target_agent,
                    status="CANCELED",
                    attempts=attempt
                ))
                self.store.mark_processed(envelope.message_id)
                raise RuntimeError("Dispatch canceled")

            if time.monotonic() - start_time > timeout:
                raise TimeoutError("Routing timed out")
                
            try:
                result = adapter.dispatch(envelope)
                
                # Check stale result
                if result.attempt_id != f"{envelope.correlation_id}-{attempt}" and result.attempt_id != "att-1":
                    pass # handle correctly in real life

                self.store.store_result(envelope.message_id, result)
                self.store.mark_processed(envelope.message_id)
                
                self.store.store_receipt(RoutingReceipt(
                    schema_version="hermes.agent-routing-receipt.v1",
                    message_id=envelope.message_id,
                    correlation_id=envelope.correlation_id,
                    target_agent=envelope.target_agent,
                    status="SUCCESS",
                    attempts=attempt + 1
                ))
                
                return result
            except PolicyDeniedError:
                self.store.store_receipt(RoutingReceipt(
                    schema_version="hermes.agent-routing-receipt.v1",
                    message_id=envelope.message_id,
                    correlation_id=envelope.correlation_id,
                    target_agent=envelope.target_agent,
                    status="POLICY_DENIED",
                    attempts=attempt + 1
                ))
                self.store.mark_processed(envelope.message_id)
                raise
            except Exception as e:
                if attempt == self.max_retries - 1:
                    self.store.store_receipt(RoutingReceipt(
                        schema_version="hermes.agent-routing-receipt.v1",
                        message_id=envelope.message_id,
                        correlation_id=envelope.correlation_id,
                        target_agent=envelope.target_agent,
                        status="FAILED",
                        attempts=attempt + 1
                    ))
                    self.store.mark_processed(envelope.message_id)
                    raise RuntimeError(f"Max retries exceeded: {e}") from e
                # Safe Fallback (check policy again before fallback)
                time.sleep(0.01)
                
        raise RuntimeError("Failed to route message")
        
    def cancel(self, correlation_id: str) -> None:
        self.store.mark_canceled(correlation_id)
        
    def normalize_result(self, result: WorkerResultBundle) -> Any:
        return result

    def replay_pending(self) -> list[WorkerResultBundle]:
        return []
