from __future__ import annotations


def synthetic_provider_token() -> str:
    return "".join((
        "sk-proj-",
        "Q7m2V9x4L6p8R3n5",
        "K1s0D4c9B2h7W6z8",
    ))


def synthetic_high_entropy_value() -> str:
    return "".join((
        "N8v3Q1k7Z5m2",
        "C9r4L6x0P8d5",
        "H2s7W1b4",
    ))


def synthetic_telegram_token() -> str:
    return "".join((
        "246813579:",
        "Q7m2V9x4L6p8R3n5K1s0D4c9B2h7W6z8",
    ))


def synthetic_private_key_block() -> str:
    return "\n".join((
        "".join(("-----BEGIN ", "PRIVATE KEY-----")),
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKQwggSgQ7m2V9x4",
        "L6p8R3n5K1s0D4c9B2h7W6z8N8v3Q1k7Z5m2C9r4L6x0P8d5",
        "".join(("-----END ", "PRIVATE KEY-----")),
    ))


def synthetic_credential_url() -> str:
    return "".join((
        "https://service-account:",
        synthetic_high_entropy_value(),
        "@example.invalid/resource",
    ))


def synthetic_assignment(
    *,
    key: str = "API_KEY",
    value: str | None = None,
) -> str:
    assigned = synthetic_provider_token() if value is None else value
    return f'{key} = "{assigned}"\n'


def marker_only_private_key_fixture() -> str:
    return "".join(('payload = b"', "-----BEGIN OPENSSH ", 'PRIVATE KEY-----"'))


def redaction_pattern_fixture() -> str:
    return "".join((
        'pattern = r"',
        "-----BEGIN[A-Z ]*PRIVATE KEY-----",
        r"[\s\S]*?",
        "-----END[A-Z ]*PRIVATE KEY-----",
        '"',
    ))
