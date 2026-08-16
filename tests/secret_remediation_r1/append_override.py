import pytest

def test_override_transform_missing_environment():
    lines = ["services:\n", "  hermes-bot:\n", "    image: x\n"]
    from ops.secret_remediation_r1.override_transform import _find_environment_block, OverrideTransformError
    with pytest.raises(OverrideTransformError, match="block not found"):
        _find_environment_block(lines)

def test_override_transform_mapping_form():
    lines = ["services:\n", "  hermes-bot:\n", "    environment:\n", "      TELEGRAM_BOT_TOKEN: val\n"]
    from ops.secret_remediation_r1.override_transform import _find_environment_block, OverrideTransformError
    with pytest.raises(OverrideTransformError, match="Mapping form environment block not supported"):
        _find_environment_block(lines)

def test_override_transform_duplicate_protected_key():
    lines = ["services:\n", "  hermes-bot:\n", "    environment:\n", "      - TELEGRAM_BOT_TOKEN=val1\n", "      - TELEGRAM_BOT_TOKEN=val2\n"]
    from ops.secret_remediation_r1.override_transform import _find_environment_block, OverrideTransformError
    with pytest.raises(OverrideTransformError, match="Duplicate protected key found"):
        _find_environment_block(lines)

def test_override_transform_ambiguous_indentation():
    lines = ["services:\n", "  hermes-bot:\n", "    environment:\n", "       - TELEGRAM_BOT_TOKEN=val1\n"]
    from ops.secret_remediation_r1.override_transform import _find_environment_block, OverrideTransformError
    with pytest.raises(OverrideTransformError, match="Ambiguous indentation"):
        _find_environment_block(lines)

def test_override_transform_malformed_entry():
    lines = ["services:\n", "  hermes-bot:\n", "    environment:\n", "      - SOME KEY=val1\n"]
    from ops.secret_remediation_r1.override_transform import _find_environment_block, OverrideTransformError
    with pytest.raises(OverrideTransformError, match="Malformed environment entry"):
        _find_environment_block(lines)
