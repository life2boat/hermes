"""Tests for override_transform module — positive and negative cases."""

import pytest
from ops.secret_remediation_r1.override_transform import (
    _find_environment_block,
    transform_override,
    OverrideTransformError,
)


# ─── Positive cases ────────────────────────────────────────────────────────


def test_override_find_block_success():
    lines = [
        "services:\n",
        "  hermes-bot:\n",
        "    environment:\n",
        "      - NORMAL_VAR=1\n",
        "      - TELEGRAM_BOT_TOKEN=secret\n",
        "      - ANOTHER=2\n",
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


# ─── Negative cases (moved from append_override.py) ────────────────────────


def test_override_transform_missing_environment():
    """hermes-bot without environment block → OverrideTransformError."""
    lines = ["services:\n", "  hermes-bot:\n", "    image: x\n"]
    with pytest.raises(OverrideTransformError, match="block not found"):
        _find_environment_block(lines)


def test_override_transform_mapping_form():
    """Mapping-form environment block is not supported."""
    lines = [
        "services:\n",
        "  hermes-bot:\n",
        "    environment:\n",
        "      TELEGRAM_BOT_TOKEN: val\n",
    ]
    with pytest.raises(
        OverrideTransformError, match="Mapping form environment block not supported"
    ):
        _find_environment_block(lines)


def test_override_transform_duplicate_protected_key():
    """Duplicate protected key in environment block → reject."""
    lines = [
        "services:\n",
        "  hermes-bot:\n",
        "    environment:\n",
        "      - TELEGRAM_BOT_TOKEN=val1\n",
        "      - TELEGRAM_BOT_TOKEN=val2\n",
    ]
    with pytest.raises(OverrideTransformError, match="Duplicate protected key found"):
        _find_environment_block(lines)


def test_override_transform_ambiguous_indentation():
    """Non-standard indentation → OverrideTransformError."""
    lines = [
        "services:\n",
        "  hermes-bot:\n",
        "    environment:\n",
        "       - TELEGRAM_BOT_TOKEN=val1\n",  # 7-space indent
    ]
    with pytest.raises(OverrideTransformError, match="Ambiguous indentation"):
        _find_environment_block(lines)


def test_override_transform_malformed_entry():
    """Environment entry with space in key name → Malformed."""
    lines = [
        "services:\n",
        "  hermes-bot:\n",
        "    environment:\n",
        "      - SOME KEY=val1\n",
    ]
    with pytest.raises(OverrideTransformError, match="Malformed environment entry"):
        _find_environment_block(lines)
