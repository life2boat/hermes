import subprocess
import json
import uuid
import time
from typing import Mapping, Any
from .adapters import AgentTransport, AgentTransportUnavailableError
from ai_engineering.supervisor.worker_result import WorkerResultBundle, deserialize_worker_result

class FakeAgentTransport:
    def __init__(self, healthy: bool = True, fake_result: dict | None = None) -> None:
        self._healthy = healthy
        self._fake_result = fake_result or {}

    def health(self) -> bool:
        return self._healthy

    def cancel(self, operation_id: str) -> Any:
        return True

    def dispatch(self, request: Mapping[str, Any], timeout: int) -> WorkerResultBundle:
        if not self._healthy:
            raise AgentTransportUnavailableError("TRANSPORT_UNAVAILABLE")

        # Build fake result
        res = {
            "schema_version": "hermes.worker-result.v1",
            "run_id": request.get("run_id", ""),
            "task_id": request.get("task_id", ""),
            "attempt_id": request.get("attempt_id", ""),
            "intent_digest": request.get("task_intent_digest", ""),
            "worker_id": request.get("worker_id", "fake-worker"),
            "repository": request.get("repository", ""),
            "base_sha": request.get("base_sha", ""),
            "status": self._fake_result.get("status", "PASS"),
            "artifacts": self._fake_result.get("artifacts", []),
            "extracted_data": self._fake_result.get("extracted_data", {}),
            "log_summary": self._fake_result.get("log_summary", "success"),
        }
        return deserialize_worker_result(json.dumps(res))

class ConfiguredLocalAgentTransport:
    def __init__(self, worker_cmd: list[str] | None = None) -> None:
        self.worker_cmd = worker_cmd

    def health(self) -> bool:
        # For Antigravity, we return False to simulate ANTIGRAVITY_TRANSPORT_UNAVAILABLE
        # unless configured with a valid command
        return self.worker_cmd is not None

    def cancel(self, operation_id: str) -> Any:
        return True

    def dispatch(self, request: Mapping[str, Any], timeout: int) -> WorkerResultBundle:
        if not self.worker_cmd:
            raise AgentTransportUnavailableError("ANTIGRAVITY_TRANSPORT_UNAVAILABLE")

        # In a real impl, we'd spawn the worker process passing the request JSON via stdin or file.
        # Here we just execute it and wait.
        try:
            req_json = json.dumps(dict(request))
            proc = subprocess.run(
                self.worker_cmd,
                input=req_json.encode("utf-8"),
                capture_output=True,
                timeout=timeout
            )
            if proc.returncode != 0:
                raise RuntimeError(f"Worker failed: {proc.stderr.decode('utf-8')}")
            return deserialize_worker_result(proc.stdout.decode("utf-8"))
        except FileNotFoundError:
            raise AgentTransportUnavailableError("TRANSPORT_UNAVAILABLE")
        except subprocess.TimeoutExpired:
            raise TimeoutError("WORK_TIMEOUT")
