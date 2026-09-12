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

import threading

class ConfiguredLocalAgentTransport:
    def __init__(self, worker_cmd: list[str] | None = None) -> None:
        self.worker_cmd = worker_cmd
        self._procs: dict[str, subprocess.Popen] = {}
        self._completed_or_cancelled: set[str] = set()
        self._lock = threading.Lock()

    def health(self) -> bool:
        return self.worker_cmd is not None

    def cancel(self, operation_id: str) -> bool:
        with self._lock:
            self._completed_or_cancelled.add(operation_id)
            proc = self._procs.pop(operation_id, None)
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return True
        return True

    def is_running(self, operation_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(operation_id)
            if not proc:
                return False
            if proc.poll() is None:
                return True
            else:
                self._procs.pop(operation_id, None)
                self._completed_or_cancelled.add(operation_id)
                return False

    def dispatch(self, request: Mapping[str, Any], timeout: int) -> WorkerResultBundle:
        from .adapters import AgentTransportUnavailableError
        if not self.worker_cmd:
            raise AgentTransportUnavailableError("ANTIGRAVITY_TRANSPORT_UNAVAILABLE")

        req_json = json.dumps(dict(request))
        operation_id = request.get("operation_id", str(uuid.uuid4()))

        try:
            import subprocess
            with self._lock:
                if operation_id in self._completed_or_cancelled:
                    raise TimeoutError("WORK_TIMEOUT")
                proc = subprocess.Popen(
                    self.worker_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                self._procs[operation_id] = proc

            stdout_data, stderr_data = proc.communicate(input=req_json, timeout=timeout)

            with self._lock:
                self._procs.pop(operation_id, None)
                self._completed_or_cancelled.add(operation_id)

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
            with self._lock:
                self._completed_or_cancelled.add(operation_id)
                proc_to_kill = self._procs.pop(operation_id, None)
            if proc_to_kill:
                proc_to_kill.terminate()
                try:
                    proc_to_kill.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc_to_kill.kill()
                    proc_to_kill.wait()
            raise TimeoutError("WORK_TIMEOUT")
