import json
import os
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Any, Optional

try:
    import fcntl
except ImportError:
    fcntl = None  # For Windows fallback


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
            return ExecutionState(**data)
        except Exception:
            return None

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
