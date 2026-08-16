import pytest

def test_runtime_invariant_legacy_image_ref_mismatch():
    from ops.secret_remediation_r1.runtime_invariant import verify_runtime_invariants, RuntimeInvariantError
    from ops.secret_remediation_r1.process_identity import DockerBackend
    class MockDocker(DockerBackend):
        def inspect(self, name):
            return [{"Config": {"Labels": {"com.docker.compose.image": "wrong:ref"}, "Image": "sha256:wrong", "Env": ["MEMORY_VECTOR_ENABLED=true", "QDRANT_ENDPOINT=http", "QDRANT_COLLECTION=healbite_memory_os"]}, "Image": "sha256:635efcd80ac8326848ed3620d5d9878971b224076c4f8694d5c22d1edfe1ed08", "Mounts": [{"Source": "/var/lib/hermes/production-db/healbite.db", "Destination": "/home/hermes/healbite.db"}]}]
    with pytest.raises(RuntimeInvariantError, match="Image ref mismatch"):
        verify_runtime_invariants(MockDocker())

def test_runtime_invariant_memory_vector_missing():
    from ops.secret_remediation_r1.runtime_invariant import verify_runtime_invariants, RuntimeInvariantError
    from ops.secret_remediation_r1.process_identity import DockerBackend
    class MockDocker(DockerBackend):
        def inspect(self, name):
            return [{"Config": {"Labels": {"com.docker.compose.image": "healbite-hermes:pr99-main-273b0a6cccaf", "com.docker.compose.project": "healbite-s72-family-invite-main", "com.docker.compose.service": "hermes-bot"}, "Image": "sha256:635efcd80ac8326848ed3620d5d9878971b224076c4f8694d5c22d1edfe1ed08", "Env": ["QDRANT_ENDPOINT=http", "QDRANT_COLLECTION=healbite_memory_os"]}, "Image": "sha256:635efcd80ac8326848ed3620d5d9878971b224076c4f8694d5c22d1edfe1ed08", "Mounts": [{"Source": "/var/lib/hermes/production-db/healbite.db", "Destination": "/home/hermes/healbite.db"}]}]
    with pytest.raises(RuntimeInvariantError, match="MEMORY_VECTOR_ENABLED mismatch"):
        verify_runtime_invariants(MockDocker())

def test_runtime_invariant_qdrant_endpoint_missing():
    from ops.secret_remediation_r1.runtime_invariant import verify_runtime_invariants, RuntimeInvariantError
    from ops.secret_remediation_r1.process_identity import DockerBackend
    class MockDocker(DockerBackend):
        def inspect(self, name):
            return [{"Config": {"Labels": {"com.docker.compose.image": "healbite-hermes:pr99-main-273b0a6cccaf", "com.docker.compose.project": "healbite-s72-family-invite-main", "com.docker.compose.service": "hermes-bot"}, "Image": "sha256:635efcd80ac8326848ed3620d5d9878971b224076c4f8694d5c22d1edfe1ed08", "Env": ["MEMORY_VECTOR_ENABLED=true", "QDRANT_COLLECTION=healbite_memory_os"]}, "Image": "sha256:635efcd80ac8326848ed3620d5d9878971b224076c4f8694d5c22d1edfe1ed08", "Mounts": [{"Source": "/var/lib/hermes/production-db/healbite.db", "Destination": "/home/hermes/healbite.db"}]}]
    with pytest.raises(RuntimeInvariantError, match="QDRANT_ENDPOINT missing or empty"):
        verify_runtime_invariants(MockDocker())
