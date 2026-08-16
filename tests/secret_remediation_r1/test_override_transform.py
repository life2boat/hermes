import pytest
from ops.secret_remediation_r1.override_transform import _find_environment_block, transform_override, OverrideTransformError

def test_override_find_block_success():
    lines = [
        "services:\n",
        "  hermes-bot:\n",
        "    environment:\n",
        "      - NORMAL_VAR=1\n",
        "      - TELEGRAM_BOT_TOKEN=secret\n",
        "      - ANOTHER=2\n"
    ]
    indices = _find_environment_block(lines)
    assert indices == [4]

def test_override_transform_success(tmp_path):
    src = tmp_path / "src.yml"
    dest = tmp_path / "dest.yml"
    content = (
        b"services:\n"
        b"  hermes-bot:\n"
        b"    environment:\n"
        b"      - NORMAL_VAR=1\n"
        b"      - TELEGRAM_BOT_TOKEN=secret\n"
        b"      - ANOTHER=2\n"
    )
    src.write_bytes(content)
    
    orig = transform_override(str(src), str(dest))
    assert orig == content
    
    expected = (
        b"services:\n"
        b"  hermes-bot:\n"
        b"    environment:\n"
        b"      - NORMAL_VAR=1\n"
        b"      - ANOTHER=2\n"
    )
    assert dest.read_bytes() == expected

def test_override_transform_preserves_unrelated(tmp_path):
    src = tmp_path / "src.yml"
    dest = tmp_path / "dest.yml"
    content = (
        b"services:\n"
        b"  hermes-bot:\n"
        b"    environment:\n"
        b"      - TELEGRAM_BOT_TOKEN=secret\n"
        b"  other-service:\n"
        b"    environment:\n"
        b"      - TELEGRAM_BOT_TOKEN=dont_touch\n"
    )
    src.write_bytes(content)
    
    transform_override(str(src), str(dest))
    
    expected = (
        b"services:\n"
        b"  hermes-bot:\n"
        b"    environment:\n"
        b"  other-service:\n"
        b"    environment:\n"
        b"      - TELEGRAM_BOT_TOKEN=dont_touch\n"
    )
    assert dest.read_bytes() == expected
