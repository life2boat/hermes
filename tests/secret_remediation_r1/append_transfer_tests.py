import pytest
from ops.secret_remediation_r1.secret_transfer import parse_protected_env, SecretTransferError

def test_secret_empty_value_reject():
    env_bytes = b"TELEGRAM_BOT_TOKEN=\x00"
    with pytest.raises(SecretTransferError, match="Empty value for protected secret: TELEGRAM_BOT_TOKEN"):
        parse_protected_env(env_bytes)
