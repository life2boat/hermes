"""FileSupervisorStateStore - atomic, single-writer event-sourced journal."""

from __future__ import annotations

import sys
if sys.platform != 'win32':
    import fcntl
    msvcrt = None
else:
    fcntl = None
    import msvcrt
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

from ai_engineering.supervisor.events import (
    SupervisorEvent,
    MAX_EVENT_PAYLOAD_BYTES,
    canonical_serialize_event,
    deserialize_event,
    validate_event_chain,
)
from ai_engineering.supervisor.replay import replay_events_with_seed
from ai_engineering.supervisor.state import (
    SupervisorError,
    SupervisorState,
    canonical_serialize_state,
    deserialize_state,
    _fail,
)

_STORE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
REPLAY_EVENTS_MAX = 10_000


class SupervisorStateStore(Protocol):
    def save_event(self, event: SupervisorEvent) -> None: ...
    def load_events(self, run_id: str) -> list[SupervisorEvent]: ...
    def load_state(self, run_id: str) -> SupervisorState: ...


def _validate_run_id_as_path(run_id: str) -> str:
    """Validate run_id is safe to use as a path component."""
    if not isinstance(run_id, str):
        _fail("STORE_RUN_ID_INVALID")
    if not _STORE_ID_RE.fullmatch(run_id):
        _fail("STORE_RUN_ID_INVALID")
    if ".." in run_id.split("/") or ".." in run_id.split(os.sep):
        _fail("STORE_PATH_TRAVERSAL")
    # Double-check for .. as substring with path separators
    normalized = run_id.replace("\\", "/").replace("\\\\", "/")
    for part in normalized.split("/"):
        if part == "..":
            _fail("STORE_PATH_TRAVERSAL")
    if run_id.startswith("/") or run_id.startswith("\\"):
        _fail("STORE_PATH_TRAVERSAL")
    if len(run_id) >= 2 and run_id[1] == ":":
        _fail("STORE_DRIVE_PATH")
    if run_id.startswith("//") or run_id.startswith("\\\\"):
        _fail("STORE_UNC_PATH")
    return run_id


class FileSupervisorStateStore:
    """Atomic, single-writer file-based event journal.

    Structure:
        state_root / run_id / events / {sequence:06d}_{event_id}.json
        state_root / run_id / seed_state.json
        state_root / run_id / writer.lock
    """

    def __init__(self, state_root: Path) -> None:
        self._root = Path(state_root)
        self._lock_fds: dict[str, int] = {}

    def _run_dir(self, run_id: str) -> Path:
        _validate_run_id_as_path(run_id)
        return self._root / run_id

    def _events_dir(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "events"

    def _lock_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "writer.lock"

    def _seed_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "seed_state.json"

    def _acquire_lock(self, run_id: str) -> None:
        """Acquire exclusive lock for single-writer guarantee."""
        lock_path = self._lock_path(run_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(fd)
            _fail("STORE_WRITER_LOCK_CONTENTION")
        self._lock_fds[run_id] = fd

    def _release_lock(self, run_id: str) -> None:
        fd = self._lock_fds.pop(run_id, None)
        if fd is not None:
            if fcntl:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            os.close(fd)

    def save_seed_state(self, state: SupervisorState) -> None:
        """Save the seed state for replay bootstrapping."""
        run_dir = self._run_dir(state.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        self._events_dir(state.run_id).mkdir(parents=True, exist_ok=True)
        seed_path = self._seed_path(state.run_id)
        content = canonical_serialize_state(state).encode("utf-8")
        self._atomic_write(seed_path, content)

    def save_event(self, event: SupervisorEvent) -> None:
        """Atomically persist an event to disk."""
        run_id = event.run_id
        events_dir = self._events_dir(run_id)
        events_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{event.sequence:06d}_{event.event_id}.json"
        target = events_dir / filename

        content = canonical_serialize_event(event).encode("utf-8")
        if len(content) > MAX_EVENT_PAYLOAD_BYTES:
            _fail("EVENT_TOO_LARGE")

        # Check for duplicate event file (idempotent write)
        if target.exists():
            existing = target.read_bytes()
            if existing == content:
                return  # identical - idempotent
            # Same name but different content - collision
            _fail("EVENT_COLLISION")

        self._atomic_write(target, content)

    def _atomic_write(self, target: Path, content: bytes) -> None:
        """Write to tmp file then os.replace for atomicity."""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent), prefix=".tmp_", suffix=".json"
        )
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(target))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load_events(self, run_id: str) -> list[SupervisorEvent]:
        """Load all events for a run, sorted by sequence."""
        _validate_run_id_as_path(run_id)
        events_dir = self._events_dir(run_id)
        if not events_dir.exists():
            return []

        files = sorted(events_dir.glob("*.json"))
        if len(files) > REPLAY_EVENTS_MAX:
            _fail("REPLAY_EVENTS_EXCEEDED")

        events: list[SupervisorEvent] = []
        for f in files:
            raw = f.read_bytes()
            if len(raw) > MAX_EVENT_PAYLOAD_BYTES:
                _fail("EVENT_TOO_LARGE")
            if not raw:
                _fail("EVENT_CORRUPT")
            try:
                event = deserialize_event(raw)
            except SupervisorError:
                raise
            except Exception as exc:
                raise SupervisorError("EVENT_CORRUPT") from exc
            events.append(event)

        if events:
            validate_event_chain(events)

        return events

    def load_seed_state(self, run_id: str) -> SupervisorState:
        """Load the seed state for replay."""
        _validate_run_id_as_path(run_id)
        seed_path = self._seed_path(run_id)
        if not seed_path.exists():
            _fail("STORE_SEED_NOT_FOUND")
        raw = seed_path.read_bytes()
        return deserialize_state(raw)

    def load_state(self, run_id: str) -> SupervisorState:
        """Load and replay events to get current state."""
        seed = self.load_seed_state(run_id)
        events = self.load_events(run_id)
        if not events:
            return seed
        return replay_events_with_seed(events, seed)
