import pytest
from ops.secret_remediation_r1.runtime_invariant import (
    verify_runtime_invariants,
    capture_runtime_prestate,
    RuntimeInvariantError,
)
from ops.secret_remediation_r1.constants import (
    CONTAINER_NAME,
    COMPOSE_PROJECT,
    COMPOSE_SERVICE,
    LEGACY_IMAGE_ID,
    DB_MOUNT_SOURCE,
    DB_MOUNT_DESTINATION,
    LEGACY_IMAGE_REF,
)
from ops.secret_remediation_r1.process_identity import DockerBackend


class MockBackend(DockerBackend):
    def __init__(self, data):
        self.data = data

    def inspect(self, name):
        return self.data


def get_base_data():
    return [
        {
            "Id": "cid",
            "State": {"Running": True},
            "Image": LEGACY_IMAGE_ID,
            "Config": {
                "Labels": {
                    "com.docker.compose.image": LEGACY_IMAGE_REF,
                    "com.docker.compose.project": COMPOSE_PROJECT,
                    "com.docker.compose.service": COMPOSE_SERVICE,
                },
                "Env": [
                    "MEMORY_VECTOR_ENABLED=true",
                    "QDRANT_ENDPOINT=http",
                    "QDRANT_COLLECTION=healbite_memory_os",
                ],
            },
            "Mounts": [
                {"Source": DB_MOUNT_SOURCE, "Destination": DB_MOUNT_DESTINATION}
            ],
        }
    ]


def test_verify_runtime_invariants_success():
    backend = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend)
    verify_runtime_invariants(expected=prestate, docker=backend)


def test_verify_runtime_invariants_wrong_image():
    backend_pre = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend_pre)
    data = get_base_data()
    data[0]["Image"] = "wrong"
    backend_post = MockBackend(data)
    with pytest.raises(RuntimeInvariantError, match="Image ID mismatch"):
        verify_runtime_invariants(expected=prestate, docker=backend_post)


def test_verify_runtime_invariants_wrong_project():
    backend_pre = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend_pre)
    data = get_base_data()
    data[0]["Config"]["Labels"]["com.docker.compose.project"] = "wrong"
    backend_post = MockBackend(data)
    with pytest.raises(RuntimeInvariantError, match="Wrong project"):
        verify_runtime_invariants(expected=prestate, docker=backend_post)


def test_verify_runtime_invariants_missing_db_mount():
    backend_pre = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend_pre)
    data = get_base_data()
    data[0]["Mounts"] = []
    backend_post = MockBackend(data)
    with pytest.raises(RuntimeInvariantError, match="DB mount not found"):
        verify_runtime_invariants(expected=prestate, docker=backend_post)


def test_runtime_invariant_legacy_image_ref_mismatch():
    backend_pre = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend_pre)
    data = get_base_data()
    data[0]["Config"]["Labels"]["com.docker.compose.image"] = "wrong:ref"
    backend_post = MockBackend(data)
    with pytest.raises(RuntimeInvariantError, match="Image ref mismatch"):
        verify_runtime_invariants(expected=prestate, docker=backend_post)


def test_runtime_invariant_memory_vector_missing():
    backend_pre = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend_pre)
    data = get_base_data()
    data[0]["Config"]["Env"] = [
        "QDRANT_ENDPOINT=http",
        "QDRANT_COLLECTION=healbite_memory_os",
    ]
    backend_post = MockBackend(data)
    with pytest.raises(RuntimeInvariantError, match="MEMORY_VECTOR_ENABLED mismatch"):
        verify_runtime_invariants(expected=prestate, docker=backend_post)


def test_runtime_invariant_qdrant_endpoint_missing():
    backend_pre = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend_pre)
    data = get_base_data()
    data[0]["Config"]["Env"] = [
        "MEMORY_VECTOR_ENABLED=true",
        "QDRANT_COLLECTION=healbite_memory_os",
    ]
    backend_post = MockBackend(data)
    with pytest.raises(RuntimeInvariantError, match="QDRANT_ENDPOINT mismatch"):
        verify_runtime_invariants(expected=prestate, docker=backend_post)


def test_runtime_prestate_capture():
    backend = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend)
    assert prestate.container_id == "cid"
    assert prestate.compose_project == COMPOSE_PROJECT
    assert prestate.compose_service == COMPOSE_SERVICE
    assert prestate.memory_vector_enabled == "true"
    assert prestate.qdrant_endpoint == "http"
    assert prestate.qdrant_collection == "healbite_memory_os"


def test_runtime_vector_changed_rejected():
    backend_pre = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend_pre)
    data = get_base_data()
    data[0]["Config"]["Env"] = [
        "MEMORY_VECTOR_ENABLED=false",
        "QDRANT_ENDPOINT=http",
        "QDRANT_COLLECTION=healbite_memory_os",
    ]
    backend_post = MockBackend(data)
    with pytest.raises(RuntimeInvariantError, match="MEMORY_VECTOR_ENABLED mismatch"):
        verify_runtime_invariants(expected=prestate, docker=backend_post)


def test_runtime_qdrant_changed_rejected():
    backend_pre = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend_pre)
    data = get_base_data()
    data[0]["Config"]["Env"] = [
        "MEMORY_VECTOR_ENABLED=true",
        "QDRANT_ENDPOINT=https://new",
        "QDRANT_COLLECTION=healbite_memory_os",
    ]
    backend_post = MockBackend(data)
    with pytest.raises(RuntimeInvariantError, match="QDRANT_ENDPOINT mismatch"):
        verify_runtime_invariants(expected=prestate, docker=backend_post)


def test_runtime_db_mount_changed_rejected():
    backend_pre = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend_pre)
    data = get_base_data()
    data[0]["Mounts"] = [{"Source": DB_MOUNT_SOURCE, "Destination": "/wrong"}]
    backend_post = MockBackend(data)
    with pytest.raises(RuntimeInvariantError, match="DB mount destination mismatch"):
        verify_runtime_invariants(expected=prestate, docker=backend_post)


def test_runtime_service_changed_rejected():
    backend_pre = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend_pre)
    data = get_base_data()
    data[0]["Config"]["Labels"]["com.docker.compose.service"] = "wrong"
    backend_post = MockBackend(data)
    with pytest.raises(RuntimeInvariantError, match="Wrong service"):
        verify_runtime_invariants(expected=prestate, docker=backend_post)


def test_runtime_project_changed_rejected():
    backend_pre = MockBackend(get_base_data())
    prestate = capture_runtime_prestate(backend_pre)
    data = get_base_data()
    data[0]["Config"]["Labels"]["com.docker.compose.project"] = "wrong"
    backend_post = MockBackend(data)
    with pytest.raises(RuntimeInvariantError, match="Wrong project"):
        verify_runtime_invariants(expected=prestate, docker=backend_post)
