"""Cross-Agent Router."""

from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Optional, Protocol, Any
from ai_engineering.supervisor.router.envelope import AgentEnvelope
from ai_engineering.supervisor.router.registry import AgentRegistry
from ai_engineering.supervisor.worker_result import WorkerResultBundle

class PersistentStore(Protocol):
    def has_processed(self, message_id: str) -> bool: ...
    def mark_processed(self, message_id: str) -> None: ...
    def store_result(self, message_id: str, result: WorkerResultBundle) -> None: ...
    def get_result(self, message_id: str) -> Optional[WorkerResultBundle]: ...
    def save_message(self, envelope: AgentEnvelope) -> None: ...
    def load_messages(self) -> list[AgentEnvelope]: ...

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
        
    def has_processed(self, message_id: str) -> bool:
        return (self.processed_dir / message_id).exists()
        
    def mark_processed(self, message_id: str) -> None:
        (self.processed_dir / message_id).touch()
        
    def store_result(self, message_id: str, result: WorkerResultBundle) -> None:
        from ai_engineering.supervisor.worker_result import canonical_serialize_worker_result
        data = canonical_serialize_worker_result(result)
        (self.results_dir / f"{message_id}.json").write_text(data, encoding="utf-8")
        
    def get_result(self, message_id: str) -> Optional[WorkerResultBundle]:
        path = self.results_dir / f"{message_id}.json"
        if not path.exists():
            return None
        from ai_engineering.supervisor.worker_result import deserialize_worker_result
        return deserialize_worker_result(path.read_text(encoding="utf-8"))
        
    def save_message(self, envelope: AgentEnvelope) -> None:
        data = json.dumps({
            "message_id": envelope.message_id,
            "correlation_id": envelope.correlation_id,
            "target_agent": envelope.target_agent,
            "reply_to": envelope.reply_to,
            "payload": envelope.payload,
            "payload_digest": envelope.payload_digest,
            "timestamp_utc": envelope.timestamp_utc
        })
        (self.messages_dir / f"{envelope.message_id}.json").write_text(data, encoding="utf-8")
        
    def load_messages(self) -> list[AgentEnvelope]:
        messages = []
        for path in self.messages_dir.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            messages.append(AgentEnvelope(**data))
        return messages


class CrossAgentRouter:
    def __init__(self, registry: AgentRegistry, store: PersistentStore, max_retries: int = 3, timeout_seconds: int = 30) -> None:
        self.registry = registry
        self.store = store
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds

    def route(self, envelope: AgentEnvelope) -> WorkerResultBundle:
        """Route message with deduplication, retry, and timeout."""
        if not envelope.verify_integrity():
            raise ValueError("Payload integrity verification failed")

        self.store.save_message(envelope)

        if self.store.has_processed(envelope.message_id):
            result = self.store.get_result(envelope.message_id)
            if result is not None:
                return result
            # Processed but no result? Should not happen if store is transactional, but for simplicity:
            pass

        adapter = self.registry.get_adapter(envelope.target_agent)
        
        start_time = time.monotonic()
        for attempt in range(self.max_retries):
            if time.monotonic() - start_time > self.timeout_seconds:
                raise TimeoutError("Routing timed out")
                
            try:
                result = adapter.dispatch(envelope)
                self.store.store_result(envelope.message_id, result)
                self.store.mark_processed(envelope.message_id)
                return result
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise RuntimeError(f"Max retries exceeded: {e}") from e
                time.sleep(1) # simple backoff
                
        raise RuntimeError("Failed to route message")
        
    def replay_pending(self) -> list[WorkerResultBundle]:
        results = []
        for msg in self.store.load_messages():
            if not self.store.has_processed(msg.message_id):
                results.append(self.route(msg))
        return results
