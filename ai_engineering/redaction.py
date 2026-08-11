"""Sanitization boundary for structured behaviour evidence."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ai_engineering.contracts import TraceValidationError
from scripts.secret_scanner import SecretScanError, scan_secret_text


REDACTED = "<REDACTED>"

_FORBIDDEN_RAW_KEYS = frozenset(
    {
        "raw_prompt",
        "prompt_text",
        "chain_of_thought",
        "private_reasoning",
        "raw_hidden_reasoning",
        "raw_user_message",
        "user_message",
        "raw_provider_response",
        "provider_response",
        "raw_tool_output",
        "raw_stdout",
        "raw_stderr",
        "raw_production_log",
        "raw_production_log_payload",
        "production_log_payload",
        "environment",
        "env",
        "credentials",
        "credential",
        "api_key",
        "token",
        "password",
        "secret",
    }
)
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "authorization_header",
        "cookie",
        "set_cookie",
    }
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


def _normalized_key(value: object) -> str:
    return str(value).strip().casefold().replace("-", "_")


def _walk_forbidden(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _normalized_key(key) in _FORBIDDEN_RAW_KEYS:
                raise TraceValidationError("TRACE_FORBIDDEN_RAW_FIELD")
            _walk_forbidden(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            _walk_forbidden(nested)


def reject_forbidden_raw_fields(value: object) -> None:
    """Reject fields that must never be representable in trace evidence."""

    _walk_forbidden(value)


def _redact_text(value: str) -> str:
    redacted = _BEARER_RE.sub(REDACTED, value)
    try:
        findings = scan_secret_text(redacted)
    except SecretScanError as exc:
        raise TraceValidationError("TRACE_VALUE_NOT_SANITIZED") from exc
    return REDACTED if findings else redacted


def sanitize_evidence(value: object) -> object:
    """Return a sanitized copy without mutating the caller's object.

    Contract-forbidden fields are rejected. Obvious sensitive values in a
    generic evidence adapter are redacted, but trace schemas remain closed and
    must validate the resulting structure separately.
    """

    reject_forbidden_raw_fields(value)

    def clean(item: object, *, key: str | None = None) -> object:
        if key is not None and _normalized_key(key) in _SENSITIVE_KEYS:
            return REDACTED
        if isinstance(item, Mapping):
            return {str(k): clean(v, key=str(k)) for k, v in item.items()}
        if isinstance(item, tuple):
            return tuple(clean(nested) for nested in item)
        if isinstance(item, list):
            return [clean(nested) for nested in item]
        if isinstance(item, str):
            return _redact_text(item)
        if isinstance(item, bytearray):
            return bytes(item)
        return item

    return clean(value)


def verify_sanitized_evidence(value: object) -> None:
    """Fail closed when structured evidence still contains sensitive content."""

    reject_forbidden_raw_fields(value)

    def verify(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if _normalized_key(key) in _SENSITIVE_KEYS and nested != REDACTED:
                    raise TraceValidationError("TRACE_VALUE_NOT_SANITIZED")
                verify(nested)
            return
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            for nested in item:
                verify(nested)
            return
        if isinstance(item, (bytes, bytearray)):
            raise TraceValidationError("TRACE_VALUE_NOT_SANITIZED")
        if isinstance(item, str):
            if _BEARER_RE.search(item):
                raise TraceValidationError("TRACE_VALUE_NOT_SANITIZED")
            try:
                findings = scan_secret_text(item)
            except SecretScanError as exc:
                raise TraceValidationError("TRACE_VALUE_NOT_SANITIZED") from exc
            if findings:
                raise TraceValidationError("TRACE_VALUE_NOT_SANITIZED")

    verify(value)


def is_sanitized_evidence(value: object) -> bool:
    try:
        verify_sanitized_evidence(value)
    except TraceValidationError:
        return False
    return True
