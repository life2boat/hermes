
def test_revalidate_second_poller_appeared(monkeypatch):
    from ops.secret_remediation_r1.process_identity import read_poller_environ, ContainerIdentity, ProcessIdentityError
    from ops.secret_remediation_r1.constants import COMPOSE_PROJECT, COMPOSE_SERVICE, LEGACY_IMAGE_ID
    import os
    class TrackBackend:
        def __init__(self):
            self.calls = 0
        def inspect(self, name):
            self.calls += 1
            return [{"Id": "cid", "State": {"Running": True, "Pid": 1}, "Image": LEGACY_IMAGE_ID, "Config": {"Labels": {"com.docker.compose.project": COMPOSE_PROJECT, "com.docker.compose.service": COMPOSE_SERVICE}}}]
        def container_pids(self, name): return []
    docker = TrackBackend()
    identity = ContainerIdentity(container_id="cid", init_pid=1, image_id=LEGACY_IMAGE_ID, running=True)
    monkeypatch.setattr(os, "lstat", lambda p: None)
    monkeypatch.setattr(os, "open", lambda p, f: 99)
    monkeypatch.setattr(os, "close", lambda fd: None)
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(os, "read", lambda fd, n: b"" if hasattr(os, "_read_done") else (setattr(os, "_read_done", True) or b"data"))
    import ops.secret_remediation_r1.process_identity as pi
    monkeypatch.setattr(pi, "_get_container_init_pid", lambda d: 1)
    monkeypatch.setattr(pi, "_get_pid_namespace", lambda p: 100)
    monkeypatch.setattr(pi, "_read_pid_cgroup", lambda p: "cid")
    
    # First it's 1, then it's 2
    def mock_find_poller():
        if docker.calls == 1:
            return [123]
        return [123, 124]
        
    monkeypatch.setattr(pi, "_find_poller_processes", mock_find_poller)
    with pytest.raises(ProcessIdentityError, match="Multiple pollers found during revalidation"):
        read_poller_environ(pid=123, identity=identity, docker=docker)
