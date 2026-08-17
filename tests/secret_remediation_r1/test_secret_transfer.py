import pytest
from ops.secret_remediation_r1.secret_transfer import (
    parse_protected_env,
    SecretTransferError,
)


def test_secret_required_telegram_missing():
    with pytest.raises(SecretTransferError, match="TELEGRAM_BOT_TOKEN not found"):
        parse_protected_env(b"NORMAL=1\x00")


def test_secret_optional_dashscope_absent():
    res = parse_protected_env(b"TELEGRAM_BOT_TOKEN=123\x00")
    assert res == [b"TELEGRAM_BOT_TOKEN=123\n"]


def test_secret_duplicate_protected_reject():
    with pytest.raises(SecretTransferError, match="Duplicate protected name"):
        parse_protected_env(b"TELEGRAM_BOT_TOKEN=1\x00TELEGRAM_BOT_TOKEN=2\x00")


def test_secret_ambiguous_nul_causes_missing_token_reject():
    # If a NUL character is inappropriately embedded within a key,
    # the parser splits it, rendering the required token malformed and thus missing.
    with pytest.raises(SecretTransferError, match="TELEGRAM_BOT_TOKEN not found"):
        parse_protected_env(b"TELEGRAM_BOT_TO\x00KEN=123\x00")


def test_secret_cr_reject():
    with pytest.raises(SecretTransferError, match="Unsafe bytes"):
        parse_protected_env(b"TELEGRAM_BOT_TOKEN=1\r2\x00")


def test_secret_lf_reject():
    with pytest.raises(SecretTransferError, match="Unsafe bytes"):
        parse_protected_env(b"TELEGRAM_BOT_TOKEN=1\n2\x00")


def test_secret_execute_required():
    res = parse_protected_env(b"TELEGRAM_BOT_TOKEN=123\x00GEMINI_API_KEY=abc\x00")
    assert set(res) == {b"TELEGRAM_BOT_TOKEN=123\n", b"GEMINI_API_KEY=abc\n"}


import pytest
from ops.secret_remediation_r1.secret_transfer import (
    parse_protected_env,
    SecretTransferError,
)


def test_secret_empty_value_reject():
    env_bytes = b"TELEGRAM_BOT_TOKEN=\x00"
    with pytest.raises(
        SecretTransferError,
        match="Empty value for protected secret: TELEGRAM_BOT_TOKEN",
    ):
        parse_protected_env(env_bytes)
