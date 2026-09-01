"""Centralized redaction policy for operator-facing output (PR-12).

Two independent defense layers:

1. Typed allowlist views (views.py) structurally exclude sensitive data.
2. This module applies an explicit sensitive-field / sensitive-value
   policy to the serialized operator dictionary before it crosses the
   operator boundary. Producer correctness is never assumed.

Name matching uses underscore-component semantics (the final component
decides for credential-style suffixes) so that descriptive fields such
as ``secret_access_authorized`` are not blanked while ``api_key``,
``auth_token`` or ``db_password`` are.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ai_engineering.observability.contracts import ObservabilityReasonCode
from ai_engineering.observability.views import REDACTED

try:
    from scripts.secret_scanner import SecretScanError, scan_secret_text
except ImportError:  # pragma: no cover - scripts package always present in repo
    def scan_secret_text(value: str) -> tuple[object, ...]:  # type: ignore[misc]
        return ()

    class SecretScanError(ValueError):  # type: ignore[no-redef]
        pass


_CREDENTIAL_FINAL_COMPONENTS = frozenset(
    {
        "secret",
        "secrets",
        "token",
        "tokens",
        "password",
        "passwd",
        "credential",
        "credentials",
        "api_key",
        "apikey",
        "private_key",
        "privatekey",
        "authorization",
        "cookie",
        "bearer",
    }
)

_FORBIDDEN_NAME_PARTS = (
    "raw_prompt",
    "prompt_text",
    "prompt_body",
    "chain_of_thought",
    "raw_hidden_reasoning",
    "raw_user_message",
    "user_message",
    "raw_provider_response",
    "raw_tool_output",
    "raw_stdout",
    "raw_stderr",
    "raw_production_log",
)

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


def _normalized(name: object) -> str:
    return str(name).strip().casefold().replace("-", "_")


def _name_is_sensitive(name: str) -> bool:
    normalized = _normalized(name)
    components = normalized.split("_")
    if not components:
        return False
    final_component = "_".join(components[-2:]) if components[-2:] else components[-1]
    if final_component in _CREDENTIAL_FINAL_COMPONENTS:
        return True
    return components[-1] in _CREDENTIAL_FINAL_COMPONENTS


def _name_is_forbidden(name: str) -> bool:
    normalized = _normalized(name)
    return any(part in normalized for part in _FORBIDDEN_NAME_PARTS)


def _value_is_secret_like(value: str) -> bool:
    if _BEARER_RE.search(value):
        return True
    try:
        return bool(scan_secret_text(value))
    except SecretScanError:
        return True


def redact_operator_dict(
    value: Any,
    *,
    _path: str = "",
) -> tuple[Any, tuple[tuple[str, str], ...]]:
    """Return a redacted deep copy plus the list of redaction records.

    Forbidden raw-prompt-style keys and credential-style names are
    replaced with ``<REDACTED>``; string values that look like secrets
    are replaced regardless of their key. The input object is never
    mutated.
    """

    records: list[tuple[str, str]] = []

    def walk(item: Any, path: str) -> Any:
        if isinstance(item, Mapping):
            out: dict[str, Any] = {}
            for key, nested in item.items():
                key_str = str(key)
                child_path = f"{path}.{key_str}" if path else key_str
                if _name_is_forbidden(key_str):
                    records.append((child_path, "RAW_PROMPT_STYLE_FIELD_SUPPRESSED"))
                    out[key_str] = REDACTED
                    continue
                if _name_is_sensitive(key_str):
                    records.append(
                        (child_path, ObservabilityReasonCode.OBSERVABILITY_REDACTION_REQUIRED.value)
                    )
                    out[key_str] = REDACTED
                    continue
                out[key_str] = walk(nested, child_path)
            return out
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            return [walk(nested, path) for nested in item]
        if isinstance(item, str):
            if _value_is_secret_like(item):
                records.append(
                    (path, ObservabilityReasonCode.OBSERVABILITY_REDACTION_REQUIRED.value)
                )
                return REDACTED
            return item
        if isinstance(item, (bytes, bytearray)):
            records.append((path, ObservabilityReasonCode.OBSERVABILITY_REDACTION_REQUIRED.value))
            return REDACTED
        return item

    return walk(value, _path), tuple(records)
