import pytest
from ops.secret_remediation_r1.compose_transform import transform_base_compose, ComposeTransformError

def test_base_exact_node_required(tmp_path):
    src = tmp_path / "docker-compose.yml"
    src.write_bytes(b"""
services:
  hermes-bot:
    env_file:
      - /home/hermes/.hermes/.env
    image: foo
""")
    dest = tmp_path / "dest.yml"
    transform_base_compose(str(src), str(dest))
    dest_str = dest.read_text()
    assert "- /etc/hermes/hermes-runtime.env" in dest_str
    assert "- /etc/hermes/hermes-production.env" in dest_str
    assert "/home/hermes/.hermes/.env" not in dest_str

def test_base_wrong_old_path_reject(tmp_path):
    src = tmp_path / "docker-compose.yml"
    src.write_bytes(b"""
services:
  hermes-bot:
    env_file:
      - /wrong/path.env
""")
    dest = tmp_path / "dest.yml"
    with pytest.raises(ComposeTransformError, match="Unexpected env_file entries"):
        transform_base_compose(str(src), str(dest))

def test_base_unsupported_shape_reject(tmp_path):
    src = tmp_path / "docker-compose.yml"
    src.write_bytes(b"""
services:
  other-bot:
    env_file:
      - /home/hermes/.hermes/.env
""")
    dest = tmp_path / "dest.yml"
    with pytest.raises(ComposeTransformError, match="services.hermes-bot.env_file block not found"):
        transform_base_compose(str(src), str(dest))

def test_base_byte_span_only_mutation(tmp_path):
    src = tmp_path / "docker-compose.yml"
    src.write_bytes(b"""# Header
services:
  hermes-bot:
    env_file:
      - /home/hermes/.hermes/.env
# Footer
""")
    dest = tmp_path / "dest.yml"
    transform_base_compose(str(src), str(dest))
    dest_str = dest.read_text()
    assert dest_str.startswith("# Header\nservices:\n  hermes-bot:\n    env_file:\n")
    assert dest_str.endswith("# Footer\n")
