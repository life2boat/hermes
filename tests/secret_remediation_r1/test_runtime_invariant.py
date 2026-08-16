import pytest
from ops.secret_remediation_r1.runtime_invariant import verify_runtime_invariants, RuntimeInvariantError
from ops.secret_remediation_r1.constants import (
    CONTAINER_NAME, COMPOSE_PROJECT, COMPOSE_SERVICE,
    LEGACY_IMAGE_ID, DB_MOUNT_SOURCE, DB_MOUNT_DESTINATION,
    LEGACY_IMAGE_REF
)
from ops.secret_remediation_r1.process_identity import DockerBackend

class MockBackend(DockerBackend):
    def __init__(self, data):
        self.data = data
    def inspect(self, name):
        return self.data

def get_base_data():
    return [{
        "Image": LEGACY_IMAGE_ID,
        "Config": {
            "Labels": {
                "com.docker.compose.image": LEGACY_IMAGE_REF,
                "com.docker.compose.project": COMPOSE_PROJECT,
                "com.docker.compose.service": COMPOSE_SERVICE
            },
            "Env": ["MEMORY_VECTOR_ENABLED=true", "QDRANT_ENDPOINT=http", "QDRANT_COLLECTION=healbite_memory_os"]
        },
        "Mounts": [
            {"Source": DB_MOUNT_SOURCE, "Destination": DB_MOUNT_DESTINATION}
        ]
    }]

def test_verify_runtime_invariants_success():
    verify_runtime_invariants(MockBackend(get_base_data()))

def test_verify_runtime_invariants_wrong_image():
    data = get_base_data()
    data[0]["Image"] = "wrong"
    with pytest.raises(RuntimeInvariantError, match="Image ID mismatch"):
        verify_runtime_invariants(MockBackend(data))

def test_verify_runtime_invariants_wrong_project():
    data = get_base_data()
    data[0]["Config"]["Labels"]["com.docker.compose.project"] = "wrong"
    with pytest.raises(RuntimeInvariantError, match="Wrong project"):
        verify_runtime_invariants(MockBackend(data))

def test_verify_runtime_invariants_missing_db_mount():
    data = get_base_data()
    data[0]["Mounts"] = []
    with pytest.raises(RuntimeInvariantError, match="DB mount not found"):
        verify_runtime_invariants(MockBackend(data))

def test_runtime_invariant_legacy_image_ref_mismatch():
    data = get_base_data()
    data[0]["Config"]["Labels"]["com.docker.compose.image"] = "wrong:ref"
    with pytest.raises(RuntimeInvariantError, match="Image ref mismatch"):
        verify_runtime_invariants(MockBackend(data))

def test_runtime_invariant_memory_vector_missing():
    data = get_base_data()
    data[0]["Config"]["Env"] = ["QDRANT_ENDPOINT=http", "QDRANT_COLLECTION=healbite_memory_os"]
    with pytest.raises(RuntimeInvariantError, match="MEMORY_VECTOR_ENABLED mismatch"):
        verify_runtime_invariants(MockBackend(data))

def test_runtime_invariant_qdrant_endpoint_missing():
    data = get_base_data()
    data[0]["Config"]["Env"] = ["MEMORY_VECTOR_ENABLED=true", "QDRANT_COLLECTION=healbite_memory_os"]
    with pytest.raises(RuntimeInvariantError, match="QDRANT_ENDPOINT missing or empty"):
        verify_runtime_invariants(MockBackend(data))
