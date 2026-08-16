import pytest
from ops.secret_remediation_r1.runtime_invariant import verify_runtime_invariants, RuntimeInvariantError
from ops.secret_remediation_r1.constants import COMPOSE_PROJECT, COMPOSE_SERVICE, LEGACY_IMAGE_ID, DB_MOUNT_SOURCE, DB_MOUNT_DESTINATION

class MockBackend:
    def __init__(self, data):
        self.data = data
    def inspect(self, name):
        return self.data
    def container_pids(self, name):
        return []

def test_verify_runtime_invariants_success():
    data = [{
        "Image": LEGACY_IMAGE_ID,
        "Config": {
            "Labels": {
                "com.docker.compose.project": COMPOSE_PROJECT,
                "com.docker.compose.service": COMPOSE_SERVICE
            }
        },
        "Mounts": [
            {"Source": DB_MOUNT_SOURCE, "Destination": DB_MOUNT_DESTINATION}
        ]
    }]
    verify_runtime_invariants(MockBackend(data))

def test_verify_runtime_invariants_wrong_image():
    data = [{
        "Image": "wrong",
        "Config": {
            "Labels": {
                "com.docker.compose.project": COMPOSE_PROJECT,
                "com.docker.compose.service": COMPOSE_SERVICE
            }
        },
        "Mounts": [
            {"Source": DB_MOUNT_SOURCE, "Destination": DB_MOUNT_DESTINATION}
        ]
    }]
    with pytest.raises(RuntimeInvariantError, match="Image ID mismatch"):
        verify_runtime_invariants(MockBackend(data))

def test_verify_runtime_invariants_wrong_project():
    data = [{
        "Image": LEGACY_IMAGE_ID,
        "Config": {
            "Labels": {
                "com.docker.compose.project": "wrong",
                "com.docker.compose.service": COMPOSE_SERVICE
            }
        },
        "Mounts": [
            {"Source": DB_MOUNT_SOURCE, "Destination": DB_MOUNT_DESTINATION}
        ]
    }]
    with pytest.raises(RuntimeInvariantError, match="Wrong project"):
        verify_runtime_invariants(MockBackend(data))

def test_verify_runtime_invariants_missing_db_mount():
    data = [{
        "Image": LEGACY_IMAGE_ID,
        "Config": {
            "Labels": {
                "com.docker.compose.project": COMPOSE_PROJECT,
                "com.docker.compose.service": COMPOSE_SERVICE
            }
        },
        "Mounts": []
    }]
    with pytest.raises(RuntimeInvariantError, match="DB mount not found"):
        verify_runtime_invariants(MockBackend(data))
