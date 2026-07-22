from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretFinding:
    rule_id: str
    match_class: str


class SecretScanError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_PRIVATE_KEY_BLOCK_RE = re.compile(
    r"-----BEGIN (?P<kind>(?:RSA |OPENSSH |EC )?)PRIVATE KEY-----"
    r"\r?\n(?P<body>[A-Za-z0-9+/=\r\n]{32,})\r?\n"
    r"-----END (?P=kind)PRIVATE KEY-----"
)
_CREDENTIAL_URL_RE = re.compile(
    r"(?i)\b(?:https?|postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)"
    r"://[^/\s:@]+:(?P<value>[^@\s/]{12,})@"
)
_QUERY_CREDENTIAL_RE = re.compile(
    r"(?i)\bhttps?://[^\s?#]+\?[^\s#]*"
    r"(?:api_?key|access_token|auth_token|secret|password)="
    r"(?P<value>[^&#\s]{12,})"
)
_CREDENTIAL_KEY_PATTERN = (
    r"[A-Za-z0-9_]*"
    r"(?:api_?key|token|secret|password|private_?key|credential)"
    r"[A-Za-z0-9_]*"
)
_ASSIGNMENT_RE = re.compile(
    rf"""(?imx)
    (?:
        ["'](?P<quoted_key>{_CREDENTIAL_KEY_PATTERN})["']\s*:
        |
        (?<![A-Za-z0-9_?&;])(?P<plain_key>{_CREDENTIAL_KEY_PATTERN})\s*=
        |
        ^[ \t]*(?P<yaml_key>{_CREDENTIAL_KEY_PATTERN})\s*:
    )
    \s*
    (?:
        "(?P<double>[^"\r\n]{{1,1024}})"
        |
        '(?P<single>[^'\r\n]{{1,1024}})'
        |
        (?P<bare>[A-Za-z0-9_:/+.-]{{1,1024}})
    )
    """
)
_TELEGRAM_TOKEN_RE = re.compile(r"^\d{8,}:[A-Za-z0-9_-]{20,}$")
_PROVIDER_TOKEN_RES = (
    re.compile(r"^sk-(?:proj-|or-v1-)?[A-Za-z0-9_-]{16,}$"),
    re.compile(r"^ghp_[A-Za-z0-9]{20,}$"),
    re.compile(r"^github_pat_[A-Za-z0-9_]{20,}$"),
    re.compile(r"^AIza[A-Za-z0-9_-]{20,}$"),
    re.compile(r"^xox[baprs]-[A-Za-z0-9-]{20,}$"),
)
_APPROVED_PLACEHOLDERS = frozenset({
    "***",
    "<api_key>",
    "<redacted>",
    "<secret>",
    "<token>",
    "[redacted]",
    "redacted",
})
_EXPLICIT_SYNTHETIC_VALUE_RE = re.compile(
    r"(?i)^(?:"
    r"changeme|dummy|example|fake|fixture|not-a-real|placeholder|sample|"
    r"test(?:[-_](?:api[-_]?key|credential|key|password|secret|token))?|"
    r"your[-_](?:api[-_]?key|credential|key|password|secret|token)"
    r")(?:[-_.:/+][A-Za-z0-9]+)*$"
)
_SYNTHETIC_TOKEN_PREFIXES = (
    "ghp_test",
    "github_pat_test",
    "sk-ant-test",
    "sk-or-v1-test",
    "sk-test-",
    "xoxb-test",
)
_SYNTHETIC_PAYLOAD_WORDS = frozenset({
    "current",
    "delegation",
    "dotenv",
    "example",
    "export",
    "fake",
    "fresh",
    "invalid",
    "key",
    "manual",
    "pass",
    "password",
    "pool",
    "pooled",
    "primary",
    "regular",
    "same",
    "secondary",
    "secret",
    "secure",
    "setup",
    "shell",
    "stale",
    "test",
    "token",
    "valid",
    "word",
    "workspace",
})
_PROVIDER_PREFIX_RE = re.compile(
    r"^(?:sk-(?:proj-|or-v1-)?|ghp_|github_pat_|AIza|xox[baprs]-)"
)
_CREDENTIAL_KEY_SUFFIXES = (
    "api_key",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
_NON_CREDENTIAL_KEY_SUFFIXES = (
    "_count",
    "_endpoint",
    "_path",
    "_suffix",
    "_tokens",
    "_uri",
    "_url",
)


def _is_credential_key(key: str) -> bool:
    normalized = key.strip("_").casefold()
    if normalized.endswith(_NON_CREDENTIAL_KEY_SUFFIXES):
        return False
    return any(
        normalized == suffix or normalized.endswith(f"_{suffix}")
        for suffix in _CREDENTIAL_KEY_SUFFIXES
    )


def _has_predictable_sequence(value: str) -> bool:
    lowered = value.casefold()
    sequences = ("0123456789", "abcdefghijklmnopqrstuvwxyz")
    has_ordered_run = any(
        sequence[index : index + 8] in lowered
        for sequence in sequences
        for index in range(len(sequence) - 7)
    )
    return has_ordered_run or re.search(r"(.)\1{5,}", lowered) is not None


def _has_explicit_synthetic_payload(value: str) -> bool:
    provider_prefixed = _PROVIDER_PREFIX_RE.match(value) is not None
    payload = _PROVIDER_PREFIX_RE.sub("", value)
    lowered = payload.casefold()
    if "tid=" in lowered and "exp=" in lowered:
        return True
    if (
        provider_prefixed
        and re.search(r"[-_.:/+]", payload)
        and any(word in lowered for word in _SYNTHETIC_PAYLOAD_WORDS)
    ):
        return True
    ordered_trigrams = (
        "abc",
        "def",
        "ghi",
        "jkl",
        "mno",
        "pqr",
        "stu",
        "vwx",
    )
    if sum(group in lowered for group in ordered_trigrams) >= 2:
        return True
    if re.fullmatch(r"(?:[A-Z]\d){4,}[A-Za-z0-9]*", payload):
        digits = "".join(re.findall(r"\d", payload))
        if digits.startswith("1234567890"):
            return True
    words = re.findall(r"[A-Za-z]+", lowered)
    known_words = [word for word in words if word in _SYNTHETIC_PAYLOAD_WORDS]
    return len(known_words) >= 2


def _is_approved_placeholder(value: str) -> bool:
    normalized = value.strip().strip("\"'").strip()
    lowered = normalized.casefold()
    if lowered in _APPROVED_PLACEHOLDERS:
        return True
    if _EXPLICIT_SYNTHETIC_VALUE_RE.fullmatch(normalized):
        return True
    if re.fullmatch(
        r"(?i)(?:sk-|ghp_|github_pat_|AIza|xox[baprs]-)?\.\.\.",
        normalized,
    ):
        return True
    if lowered.startswith(_SYNTHETIC_TOKEN_PREFIXES):
        return True
    if _has_predictable_sequence(normalized):
        return True
    if _has_explicit_synthetic_payload(normalized):
        return True
    jwt_parts = normalized.split(".")
    if len(jwt_parts) == 3 and (len(jwt_parts[0]) < 8 or len(jwt_parts[2]) < 8):
        return True
    if re.search(
        r"[A-Z][A-Z0-9_]*(?:API_KEY|BASE_URL|TOKEN|SECRET|PASSWORD)$",
        normalized,
    ):
        return True
    provider_payload = re.sub(
        r"^(?:sk-(?:proj-|or-v1-)?|ghp_|github_pat_|AIza|xox[baprs]-)",
        "",
        normalized,
    )
    if len(provider_payload) >= 12 and len(set(provider_payload.casefold())) <= 2:
        return True
    compact = re.sub(r"[^A-Za-z0-9]", "", normalized)
    return len(compact) >= 12 and len(set(compact.casefold())) <= 2


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def _is_high_entropy_credential(value: str) -> bool:
    if len(value) < 24 or any(character.isspace() for character in value):
        return False
    if not any(character.isalpha() for character in value):
        return False
    if not any(character.isdigit() for character in value):
        return False
    return len(set(value)) >= 10 and _entropy(value) >= 3.5


def _assignment_finding(key: str, value: str) -> SecretFinding | None:
    if not _is_credential_key(key):
        return None
    candidate = value.strip()
    if _is_approved_placeholder(candidate):
        return None
    if _TELEGRAM_TOKEN_RE.fullmatch(candidate):
        return SecretFinding(
            rule_id="telegram-token-assignment",
            match_class="KNOWN_PROVIDER_CREDENTIAL",
        )
    if any(pattern.fullmatch(candidate) for pattern in _PROVIDER_TOKEN_RES):
        return SecretFinding(
            rule_id="provider-token-assignment",
            match_class="KNOWN_PROVIDER_CREDENTIAL",
        )
    if _is_high_entropy_credential(candidate):
        return SecretFinding(
            rule_id="high-entropy-secret-assignment",
            match_class="HIGH_ENTROPY_CREDENTIAL",
        )
    return None


def scan_secret_text(text: str) -> tuple[SecretFinding, ...]:
    findings: list[SecretFinding] = []
    seen: set[tuple[str, str]] = set()

    def add(finding: SecretFinding) -> None:
        identity = (finding.rule_id, finding.match_class)
        if identity not in seen:
            seen.add(identity)
            findings.append(finding)

    if _PRIVATE_KEY_BLOCK_RE.search(text):
        add(
            SecretFinding(
                rule_id="private-key-block",
                match_class="PRIVATE_KEY_MATERIAL",
            )
        )

    for pattern in (_CREDENTIAL_URL_RE, _QUERY_CREDENTIAL_RE):
        for match in pattern.finditer(text):
            value = match.group("value")
            if _assignment_finding("url_credential", value) is not None:
                add(
                    SecretFinding(
                        rule_id="credential-bearing-url",
                        match_class="CREDENTIAL_IN_URL",
                    )
                )

    for match in _ASSIGNMENT_RE.finditer(text):
        value = (
            match.group("double") or match.group("single") or match.group("bare") or ""
        )
        key = (
            match.group("quoted_key")
            or match.group("plain_key")
            or match.group("yaml_key")
            or ""
        )
        finding = _assignment_finding(key, value)
        if finding is not None:
            add(finding)

    return tuple(findings)


def scan_secret_bytes(data: bytes) -> tuple[SecretFinding, ...]:
    if b"\x00" in data:
        raise SecretScanError("SECRET_SCAN_BINARY_DENIED")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretScanError("SECRET_SCAN_DECODING_FAILED") from exc
    return scan_secret_text(text)
