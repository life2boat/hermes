from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

_SQLITE_MAX_INTEGER = 9223372036854775807
_TRUE_TOKENS = {"1", "true", "yes", "on"}
_FALSE_TOKENS = {"0", "false", "no", "off"}


class FeatureAvailabilityStatus(str, Enum):
    DISABLED = "disabled"
    MISCONFIGURED = "misconfigured"
    INVALID_ACTOR = "invalid_actor"
    NOT_ALLOWLISTED = "not_allowlisted"
    HOUSEHOLD_UNAVAILABLE = "household_unavailable"
    SCHEMA_UNAVAILABLE = "schema_unavailable"
    READY = "ready"


@dataclass(frozen=True, slots=True)
class FeatureGateConfig:
    enabled: bool = False
    allowlist: frozenset[int] = frozenset()
    configuration_valid: bool = True


@dataclass(frozen=True, slots=True)
class FeatureGateDecision:
    status: FeatureAvailabilityStatus
    enabled: bool = False
    allowlist_count: int = 0
    configuration_valid: bool = True
    actor_user_id: int | None = field(default=None, repr=False)

    @property
    def ready(self) -> bool:
        return self.status is FeatureAvailabilityStatus.READY


def _parse_enabled(value: str | None) -> tuple[bool, bool]:
    token = str(value or "").strip().lower()
    if token == "":
        return False, True
    if token in _TRUE_TOKENS:
        return True, True
    if token in _FALSE_TOKENS:
        return False, True
    return False, False


def _parse_allowlist(value: str | None) -> tuple[frozenset[int], bool]:
    entries: set[int] = set()
    for part in (value or "").replace(";", ",").split(","):
        token = part.strip()
        if not token:
            continue
        if not token.isdigit():
            return frozenset(), False
        parsed = int(token)
        if parsed <= 0 or parsed > _SQLITE_MAX_INTEGER:
            return frozenset(), False
        entries.add(parsed)
    return frozenset(entries), True


def normalize_actor_user_id(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed > _SQLITE_MAX_INTEGER:
        return None
    return parsed


def load_feature_gate_config(prefix: str, env: Mapping[str, str] | None = None) -> FeatureGateConfig:
    source = env if env is not None else os.environ
    enabled, enabled_valid = _parse_enabled(source.get(f"{prefix}_ENABLED"))
    allowlist, allowlist_valid = _parse_allowlist(source.get(f"{prefix}_ALLOWLIST"))
    if not enabled_valid or not allowlist_valid:
        return FeatureGateConfig(enabled=False, allowlist=frozenset(), configuration_valid=False)
    return FeatureGateConfig(enabled=enabled, allowlist=allowlist, configuration_valid=True)


def evaluate_feature_gate(config: FeatureGateConfig, actor_user_id: object) -> FeatureGateDecision:
    if not config.configuration_valid:
        return FeatureGateDecision(
            status=FeatureAvailabilityStatus.MISCONFIGURED,
            enabled=False,
            allowlist_count=0,
            configuration_valid=False,
        )
    if not config.enabled:
        return FeatureGateDecision(
            status=FeatureAvailabilityStatus.DISABLED,
            enabled=False,
            allowlist_count=len(config.allowlist),
            configuration_valid=True,
        )
    actor = normalize_actor_user_id(actor_user_id)
    if actor is None:
        return FeatureGateDecision(
            status=FeatureAvailabilityStatus.INVALID_ACTOR,
            enabled=True,
            allowlist_count=len(config.allowlist),
            configuration_valid=True,
        )
    if actor not in config.allowlist:
        return FeatureGateDecision(
            status=FeatureAvailabilityStatus.NOT_ALLOWLISTED,
            enabled=True,
            allowlist_count=len(config.allowlist),
            configuration_valid=True,
            actor_user_id=actor,
        )
    return FeatureGateDecision(
        status=FeatureAvailabilityStatus.READY,
        enabled=True,
        allowlist_count=len(config.allowlist),
        configuration_valid=True,
        actor_user_id=actor,
    )
