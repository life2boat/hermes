from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

from gateway.healbite_households import (
    HealBiteHouseholdService,
    HealBiteHouseholdStore,
    HouseholdAccessError,
    HouseholdContext,
    HouseholdFeatureConfig,
    HouseholdIntegrityError,
    HouseholdNotFoundError,
    HouseholdValidationError,
    PersonalHousehold,
    load_household_feature_config,
)
from gateway.healbite_nutrition_diary import resolve_healbite_db_path

_SQLITE_MAX_INTEGER = 9223372036854775807


class HouseholdRuntimeStatus(str, Enum):
    RESOLVED = "resolved"
    CREATED = "created"
    DISABLED = "disabled"
    INVALID_CONFIG = "invalid_config"
    INVALID_ACTOR = "invalid_actor"
    NOT_ALLOWLISTED = "not_allowlisted"
    ACTOR_NOT_FOUND = "actor_not_found"
    HOUSEHOLD_NOT_FOUND = "household_not_found"
    ACCESS_DENIED = "access_denied"
    SCHEMA_UNAVAILABLE = "schema_unavailable"
    INTEGRITY_ERROR = "integrity_error"
    STORE_ERROR = "store_error"


@dataclass(frozen=True, slots=True)
class HouseholdRuntimeFeatureState:
    enabled: bool = False
    allowlist_count: int = 0
    configuration_valid: bool = True


@dataclass(frozen=True, slots=True)
class HouseholdRuntimeResult:
    status: HouseholdRuntimeStatus
    context: HouseholdContext | None = field(default=None, repr=False)
    created: bool = False

    @property
    def resolved(self) -> bool:
        return self.status in {HouseholdRuntimeStatus.RESOLVED, HouseholdRuntimeStatus.CREATED}


StoreFactory = Callable[[], HealBiteHouseholdStore]


def _normalize_actor_id(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed > _SQLITE_MAX_INTEGER:
        return None
    return parsed


class HouseholdRuntimeBridge:
    def __init__(
        self,
        *,
        config: HouseholdFeatureConfig | None = None,
        db_path: str | Path | None = None,
        store_factory: StoreFactory | None = None,
    ) -> None:
        self._config = config if config is not None else load_household_feature_config()
        self._db_path = resolve_healbite_db_path(db_path)
        self._store_factory = store_factory or self._default_store_factory

    def __repr__(self) -> str:
        state = self.feature_state
        return (
            "HouseholdRuntimeBridge("
            f"enabled={state.enabled!r}, "
            f"allowlist_count={state.allowlist_count!r}, "
            f"configuration_valid={state.configuration_valid!r})"
        )

    @property
    def feature_state(self) -> HouseholdRuntimeFeatureState:
        return HouseholdRuntimeFeatureState(
            enabled=bool(self._config.enabled),
            allowlist_count=len(self._config.allowlist),
            configuration_valid=bool(self._config.allowlist_valid),
        )

    def _default_store_factory(self) -> HealBiteHouseholdStore:
        return HealBiteHouseholdStore(db_path=self._db_path, ensure_schema_on_init=False)

    def _eligible_actor(self, actor_user_id: object) -> tuple[int | None, HouseholdRuntimeStatus | None]:
        actor = _normalize_actor_id(actor_user_id)
        if actor is None:
            return None, HouseholdRuntimeStatus.INVALID_ACTOR
        if not self._config.allowlist_valid:
            return actor, HouseholdRuntimeStatus.INVALID_CONFIG
        if not self._config.enabled:
            return actor, HouseholdRuntimeStatus.DISABLED
        if actor not in self._config.allowlist:
            return actor, HouseholdRuntimeStatus.NOT_ALLOWLISTED
        return actor, None

    def is_actor_eligible(self, actor_user_id: object) -> bool:
        _actor, status = self._eligible_actor(actor_user_id)
        return status is None

    def resolve_existing_context_for_actor(self, actor_user_id: object) -> HouseholdRuntimeResult:
        actor, status = self._eligible_actor(actor_user_id)
        if status is not None:
            return HouseholdRuntimeResult(status=status)
        assert actor is not None
        try:
            service = HealBiteHouseholdService(self._store_factory())
            context = service.resolve_existing_actor_household_context(actor)
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.RESOLVED, context=context)
        except HouseholdValidationError:
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.ACTOR_NOT_FOUND)
        except HouseholdNotFoundError:
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.HOUSEHOLD_NOT_FOUND)
        except HouseholdAccessError:
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.ACCESS_DENIED)
        except HouseholdIntegrityError:
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.INTEGRITY_ERROR)
        except sqlite3.OperationalError:
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.SCHEMA_UNAVAILABLE)
        except sqlite3.Error:
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.STORE_ERROR)

    def resolve_or_create_context_for_internal_actor(self, actor_user_id: object) -> HouseholdRuntimeResult:
        actor, status = self._eligible_actor(actor_user_id)
        if status is not None:
            return HouseholdRuntimeResult(status=status)
        assert actor is not None
        try:
            service = HealBiteHouseholdService(self._store_factory())
            personal: PersonalHousehold = service.get_or_create_personal_household_for_actor(actor)
            context = HouseholdContext(
                actor_user_id=actor,
                household_id=personal.household.id,
                household_member_id=personal.member.id,
                role=personal.member.role,
                member_status=personal.member.status,
                household_status=personal.household.status,
            )
            return HouseholdRuntimeResult(
                status=HouseholdRuntimeStatus.CREATED if personal.created else HouseholdRuntimeStatus.RESOLVED,
                context=context,
                created=bool(personal.created),
            )
        except HouseholdValidationError:
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.ACTOR_NOT_FOUND)
        except HouseholdAccessError:
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.ACCESS_DENIED)
        except HouseholdIntegrityError:
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.INTEGRITY_ERROR)
        except sqlite3.OperationalError:
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.SCHEMA_UNAVAILABLE)
        except sqlite3.Error:
            return HouseholdRuntimeResult(status=HouseholdRuntimeStatus.STORE_ERROR)


def build_household_runtime_bridge(
    *,
    env: dict[str, str] | None = None,
    db_path: str | Path | None = None,
    store_factory: StoreFactory | None = None,
) -> HouseholdRuntimeBridge:
    return HouseholdRuntimeBridge(
        config=load_household_feature_config(env),
        db_path=db_path,
        store_factory=store_factory,
    )
