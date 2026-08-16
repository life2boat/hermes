import pytest
from ops.secret_remediation_r1.process_identity import resolve_poller_pid, ProcessIdentityError
from ops.secret_remediation_r1.constants import COMPOSE_PROJECT, COMPOSE_SERVICE, LEGACY_IMAGE_ID

class MockDockerBackend:
    def __init__(self, containers, pids):
        self._containers = containers
        self._pids = pids
    def inspect(self, name):
        return self._containers
    def container_pids(self, cid):
        return self._pids

def test_pid_wrong_project_reject(monkeypatch):
    backend = MockDockerBackend([{"Id": "123", "State": {"Running": True, "Pid": 1}, "Image": LEGACY_IMAGE_ID, "Config": {"Labels": {"com.docker.compose.project": "wrong", "com.docker.compose.service": COMPOSE_SERVICE}}}], [1])
    with pytest.raises(ProcessIdentityError, match="Wrong compose project"):
        resolve_poller_pid(docker=backend)

def test_pid_wrong_service_reject(monkeypatch):
    backend = MockDockerBackend([{"Id": "123", "State": {"Running": True, "Pid": 1}, "Image": LEGACY_IMAGE_ID, "Config": {"Labels": {"com.docker.compose.project": COMPOSE_PROJECT, "com.docker.compose.service": "wrong"}}}], [1])
    with pytest.raises(ProcessIdentityError, match="Wrong compose service"):
        resolve_poller_pid(docker=backend)

def test_pid_wrong_image_reject(monkeypatch):
    backend = MockDockerBackend([{"Id": "123", "State": {"Running": True, "Pid": 1}, "Image": "wrong", "Config": {"Labels": {"com.docker.compose.project": COMPOSE_PROJECT, "com.docker.compose.service": COMPOSE_SERVICE}}}], [1])
    with pytest.raises(ProcessIdentityError, match="Wrong image ID"):
        resolve_poller_pid(docker=backend)

def test_pid_not_running_reject():
    backend = MockDockerBackend([{"Id": "123", "State": {"Running": False, "Pid": 1}, "Image": LEGACY_IMAGE_ID, "Config": {"Labels": {"com.docker.compose.project": COMPOSE_PROJECT, "com.docker.compose.service": COMPOSE_SERVICE}}}], [1])
    with pytest.raises(ProcessIdentityError, match="Container is not running"):
        resolve_poller_pid(docker=backend)

def test_pid_zero_poller_reject(monkeypatch):
    backend = MockDockerBackend([{"Id": "123", "State": {"Running": True, "Pid": 1}, "Image": LEGACY_IMAGE_ID, "Config": {"Labels": {"com.docker.compose.project": COMPOSE_PROJECT, "com.docker.compose.service": COMPOSE_SERVICE}}}], [1])
    import ops.secret_remediation_r1.process_identity
    monkeypatch.setattr(ops.secret_remediation_r1.process_identity, "_find_poller_processes", lambda: [])
    with pytest.raises(ProcessIdentityError, match="No hermes gateway poller found"):
        resolve_poller_pid(docker=backend)

def test_pid_multi_poller_reject(monkeypatch):
    backend = MockDockerBackend([{"Id": "123", "State": {"Running": True, "Pid": 1}, "Image": LEGACY_IMAGE_ID, "Config": {"Labels": {"com.docker.compose.project": COMPOSE_PROJECT, "com.docker.compose.service": COMPOSE_SERVICE}}}], [1])
    import ops.secret_remediation_r1.process_identity
    monkeypatch.setattr(ops.secret_remediation_r1.process_identity, "_find_poller_processes", lambda: [100, 101])
    with pytest.raises(ProcessIdentityError, match="Multiple pollers found"):
        resolve_poller_pid(docker=backend)


def test_read_poller_environ_ordering(monkeypatch):
    from ops.secret_remediation_r1.process_identity import read_poller_environ, ContainerIdentity, ProcessIdentityError
    
    class TrackBackend:
        def __init__(self):
            self.calls = 0
            
        def inspect(self, name):
            self.calls += 1
            if self.calls == 1:
                return [{"Id": "cid", "State": {"Running": True}}]
            elif self.calls == 2:
                # second call fails (e.g. stopped)
                return [{"Id": "cid", "State": {"Running": False}}]
            return []
            
        def container_pids(self, name): return []
        
    docker = TrackBackend()
    identity = ContainerIdentity(container_id="cid", init_pid=1, image_id="img", running=True)
    
    # mock os.read etc
    import os
    monkeypatch.setattr(os, "lstat", lambda p: None)
    monkeypatch.setattr(os, "open", lambda p, f: 99)
    monkeypatch.setattr(os, "close", lambda fd: None)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None) # _pid_exists passes
    
    # Track reads
    def mock_read(fd, n):
        if not hasattr(mock_read, "calls"):
            mock_read.calls = 0
        mock_read.calls += 1
        if mock_read.calls == 1:
            # ensure _revalidate was called BEFORE read
            assert docker.calls == 1
            return b"data"
        return b""
    monkeypatch.setattr(os, "read", mock_read)
    
    with pytest.raises(ProcessIdentityError, match="Container stopped during operation"):
        read_poller_environ(pid=123, identity=identity, docker=docker)
        
    # ensure _revalidate was called AFTER read as well
    assert docker.calls == 2
    assert mock_read.calls == 2
