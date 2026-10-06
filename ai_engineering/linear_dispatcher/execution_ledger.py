import json
import os
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any, Optional

try:
    import fcntl
except ImportError:
    fcntl = None  # For Windows fallback


VALID_TASK_STATES = {
    "DISCOVERED",
    "CLAIMED",
    "WORKTREE_READY",
    "BRANCH_CREATED",
    "RUNNING",
    "VALIDATED",
    "COMMITTED",
    "BRANCH_PUSHED",
    "PR_OPEN",
    "CI_PENDING",
    "CI_PASS",
    "WRITEBACK_DONE",
    "DONE",
    "BLOCKED",
    "FAILED",
}


@dataclass
class ExecutionState:
    task_id: str
    state: str
    claim_owner: str
    claim_token: str
    branch: Optional[str] = None
    worktree_path: Optional[str] = None
    base_sha: Optional[str] = None
    head_sha: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    execution_id: Optional[str] = None


class CorruptedLedgerError(RuntimeError):
    """Raised when durable ledger state file is corrupt or invalid."""
    pass


class ExecutionLedger:
    def __init__(self, persistence_path: Path | str) -> None:
        self.persistence_path = Path(persistence_path).resolve()
        self.persistence_path.parent.mkdir(parents=True, exist_ok=True)

    def write_state(self, state: ExecutionState) -> None:
        tmp_path = self.persistence_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(asdict(state), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.persistence_path)

    def read_state(self) -> Optional[ExecutionState]:
        if not self.persistence_path.exists():
            return None
        try:
            with open(self.persistence_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("Ledger payload is not a dictionary")
            required_fields = ("task_id", "state", "claim_owner", "claim_token")
            for req in required_fields:
                if req not in data or not isinstance(data[req], str) or not data[req].strip():
                    raise ValueError(f"Missing or invalid required field '{req}'")

            if data["state"] not in VALID_TASK_STATES:
                raise ValueError(f"Invalid state '{data['state']}'")

            import re
            hex_40_pattern = re.compile(r"^[0-9a-fA-F]{40}$")

            if "head_sha" in data and data["head_sha"] is not None:
                if not isinstance(data["head_sha"], str) or not data["head_sha"].strip():
                    raise ValueError(f"Invalid head_sha: {data['head_sha']}")

            if "base_sha" in data and data["base_sha"] is not None:
                if not isinstance(data["base_sha"], str):
                    raise ValueError(f"Invalid base_sha: {data['base_sha']}")

            if "pr_number" in data and data["pr_number"] is not None:
                if not isinstance(data["pr_number"], int) or isinstance(data["pr_number"], bool):
                    raise ValueError(f"Invalid pr_number: {data['pr_number']}")

            if "pr_url" in data and data["pr_url"] is not None:
                if not isinstance(data["pr_url"], str):
                    raise ValueError(f"Invalid pr_url: {data['pr_url']}")

            if "branch" in data and data["branch"] is not None:
                if not isinstance(data["branch"], str):
                    raise ValueError(f"Invalid branch: {data['branch']}")

            if "worktree_path" in data and data["worktree_path"] is not None:
                if not isinstance(data["worktree_path"], str):
                    raise ValueError(f"Invalid worktree_path: {data['worktree_path']}")

            if "execution_id" in data and data["execution_id"] is not None:
                if not isinstance(data["execution_id"], str):
                    raise ValueError(f"Invalid execution_id: {data['execution_id']}")

            allowed_fields = {
                "task_id", "state", "claim_owner", "claim_token",
                "branch", "worktree_path", "base_sha", "head_sha",
                "pr_number", "pr_url", "execution_id"
            }
            extra_fields = set(data.keys()) - allowed_fields
            if extra_fields:
                raise ValueError(f"Unrecognized fields in ledger: {extra_fields}")

            return ExecutionState(**data)
        except Exception as exc:
            raise CorruptedLedgerError(f"MALFORMED_LEDGER_STATE: {exc}") from exc

    def clear(self) -> None:
        if self.persistence_path.exists():
            self.persistence_path.unlink()


class SingleWorkerLock:
    def __init__(self, lock_path: Path | str) -> None:
        self.lock_path = Path(lock_path).resolve()
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._raw_fd: int | None = None

    def acquire(self) -> bool:
        try:
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOINHERIT"):
                flags |= os.O_NOINHERIT
            self._raw_fd = os.open(str(self.lock_path), flags, 0o600)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._raw_fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._raw_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (IOError, OSError):
            if self._raw_fd is not None:
                try:
                    os.close(self._raw_fd)
                except Exception:
                    pass
                self._raw_fd = None
            return False

    def release(self) -> None:
        if self._raw_fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._raw_fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._raw_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(self._raw_fd)
            except Exception:
                pass
            self._raw_fd = None
