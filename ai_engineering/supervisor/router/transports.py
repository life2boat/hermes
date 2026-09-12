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
            "result_id": str(__import__('uuid').uuid4()),
            "task_id": request.get("task_id", ""),
            "attempt_id": request.get("attempt_id", ""),
            "worker_id": request.get("worker_id", "fake-worker"),
            "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "canonical_remote": "github",
            "repository": "github",
            "intent_digest": request.get("task_intent_digest", ""),
            "produced_at_utc": "2026-01-01T00:00:00Z",
            "artifacts": [],
            "gate_claims": []
        }
        return deserialize_worker_result(json.dumps(res))

class ConfiguredLocalAgentTransport:
    def __init__(self, worker_cmd: list[str] | None = None) -> None:
        self.worker_cmd = worker_cmd
        self._procs: dict[str, subprocess.Popen] = {}

    def health(self) -> bool:
        return self.worker_cmd is not None

    def cancel(self, operation_id: str) -> bool:
        if operation_id in self._procs:
            proc = self._procs[operation_id]
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            self._procs.pop(operation_id, None)
            return True
        return False

    def is_running(self, operation_id: str) -> bool:
        if operation_id in self._procs:
            proc = self._procs[operation_id]
            if proc.poll() is None:
                return True
            else:
                del self._procs[operation_id]
        return False

    def dispatch(self, request: Mapping[str, Any], timeout: int) -> WorkerResultBundle:
        from .adapters import AgentTransportUnavailableError
        if not self.worker_cmd:
            raise AgentTransportUnavailableError("ANTIGRAVITY_TRANSPORT_UNAVAILABLE")

        req_json = json.dumps(dict(request))
        operation_id = request.get("operation_id", str(uuid.uuid4()))
        
        try:
            import subprocess
            proc = subprocess.Popen(
                self.worker_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            self._procs[operation_id] = proc
            
            stdout_data, stderr_data = proc.communicate(input=req_json, timeout=timeout)
            
            if operation_id in self._procs:
                del self._procs[operation_id]
                
            if proc.returncode != 0:
                raise RuntimeError(f"Worker failed: {stderr_data}")
            
            try:
                res_dict = json.loads(stdout_data)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Worker did not output valid JSON: {e}, output: {stdout_data}")

            return deserialize_worker_result(json.dumps(res_dict))

        except FileNotFoundError:
            raise AgentTransportUnavailableError("TRANSPORT_UNAVAILABLE")
        except subprocess.TimeoutExpired:
            if operation_id in self._procs:
                proc = self._procs[operation_id]
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                del self._procs[operation_id]
            raise TimeoutError("WORK_TIMEOUT")
