from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

from gateway.healbite_feature_gates import (
    FeatureAvailabilityStatus,
    FeatureGateConfig,
    FeatureGateDecision,
    evaluate_feature_gate,
    load_feature_gate_config,
)
from gateway.healbite_households import (
    HealBiteHouseholdService,
    HealBiteHouseholdStore,
    HouseholdAccessError,
    HouseholdIntegrityError,
    HouseholdNotFoundError,
    HouseholdValidationError,
)
from gateway.healbite_nutrition_diary import resolve_healbite_db_path
from gateway.healbite_runtime_resources import RuntimeResource, borrowed_runtime_resource
from gateway.healbite_weekly_menu_schema import WeeklyMenuSchemaState
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    HouseholdAuthorizationContext,
    WeeklyMenuAccessError,
    WeeklyMenuNotFoundError,
    WeeklyMenuRevision,
    WeeklyMenuRevisionView,
    WeeklyMenuSchemaError,
    WeeklyMenuSeries,
    WeeklyMenuStateError,
    WeeklyMenuValidationError,
)


@dataclass(frozen=True, slots=True)
class WeeklyMenuRuntimeAvailability:
    status: FeatureAvailabilityStatus
    enabled: bool = False
    allowlist_count: int = 0
    configuration_valid: bool = True
    household_ready: bool = False
    schema_ready: bool = False

    @property
    def ready(self) -> bool:
        return self.status is FeatureAvailabilityStatus.READY


@dataclass(frozen=True, slots=True)
class WeeklyMenuWeekView:
    series: WeeklyMenuSeries
    revisions: tuple[WeeklyMenuRevision, ...]


class WeeklyMenuRuntimeError(Exception):
    pass


class WeeklyMenuRuntimeUnavailableError(WeeklyMenuRuntimeError):
    def __init__(self, availability: WeeklyMenuRuntimeAvailability) -> None:
        super().__init__("weekly menu runtime unavailable")
        self.availability = availability


class WeeklyMenuRuntimeNotFoundError(WeeklyMenuRuntimeError):
    pass


class WeeklyMenuRuntimeStateError(WeeklyMenuRuntimeError):
    pass


class WeeklyMenuRuntimeCleanupError(WeeklyMenuRuntimeStateError):
    pass


HouseholdStoreResourceFactory = Callable[[], RuntimeResource[HealBiteHouseholdStore]]
WeeklyMenuStoreResourceFactory = Callable[[], RuntimeResource[HealBiteWeeklyMenuStore]]
T = TypeVar("T")



def _availability_from_decision(
    decision: FeatureGateDecision,
    *,
    household_ready: bool = False,
    schema_ready: bool = False,
    status: FeatureAvailabilityStatus | None = None,
) -> WeeklyMenuRuntimeAvailability:
    return WeeklyMenuRuntimeAvailability(
        status=status or decision.status,
        enabled=decision.enabled,
        allowlist_count=decision.allowlist_count,
        configuration_valid=decision.configuration_valid,
        household_ready=household_ready,
        schema_ready=schema_ready,
    )


class HealBiteWeeklyMenuRuntimeService:
    def __init__(
        self,
        *,
        config: FeatureGateConfig | None = None,
        db_path: str | Path | None = None,
        household_store_factory: HouseholdStoreResourceFactory | None = None,
        weekly_menu_store_factory: WeeklyMenuStoreResourceFactory | None = None,
    ) -> None:
        self._config = config if config is not None else load_feature_gate_config("HEALBITE_WEEKLY_MENU")
        self._db_path = resolve_healbite_db_path(db_path)
        self._household_store_factory = household_store_factory or self._default_household_store_factory
        self._weekly_menu_store_factory = weekly_menu_store_factory or self._default_weekly_menu_store_factory

    def _default_household_store_factory(self) -> RuntimeResource[HealBiteHouseholdStore]:
        return borrowed_runtime_resource(HealBiteHouseholdStore(db_path=self._db_path, ensure_schema_on_init=False))

    def _default_weekly_menu_store_factory(self) -> RuntimeResource[HealBiteWeeklyMenuStore]:
        return borrowed_runtime_resource(HealBiteWeeklyMenuStore(db_path=self._db_path))

    def _evaluate_gate(self, actor_user_id: object) -> FeatureGateDecision:
        return evaluate_feature_gate(self._config, actor_user_id)

    def _resolve_authorization_context(self, actor_user_id: object) -> tuple[HouseholdAuthorizationContext, WeeklyMenuRuntimeAvailability]:
        decision = self._evaluate_gate(actor_user_id)
        if not decision.ready:
            raise WeeklyMenuRuntimeUnavailableError(_availability_from_decision(decision))
        assert decision.actor_user_id is not None
        resource = self._household_store_factory()
        try:
            with resource as household_store:
                service = HealBiteHouseholdService(household_store)
                context = service.resolve_existing_actor_household_context(decision.actor_user_id)
        except (HouseholdValidationError, HouseholdNotFoundError, HouseholdAccessError, HouseholdIntegrityError, sqlite3.Error):
            raise WeeklyMenuRuntimeUnavailableError(
                _availability_from_decision(
                    decision,
                    status=FeatureAvailabilityStatus.HOUSEHOLD_UNAVAILABLE,
                )
            ) from None
        self._raise_cleanup_error(resource, WeeklyMenuRuntimeCleanupError("weekly menu runtime cleanup failure"))
        return (
            HouseholdAuthorizationContext.from_household_context(context),
            _availability_from_decision(decision, household_ready=True),
        )

    def _raise_cleanup_error(self, resource: RuntimeResource[object], error: WeeklyMenuRuntimeCleanupError) -> None:
        if resource.cleanup_error is not None:
            raise error from None

    def _with_store(self, actor_user_id: object, operation: Callable[[HouseholdAuthorizationContext, HealBiteWeeklyMenuStore], T]) -> T:
        context, availability = self._resolve_authorization_context(actor_user_id)
        resource = self._weekly_menu_store_factory()
        try:
            with resource as store:
                state = store.schema_state()
                if state is not WeeklyMenuSchemaState.CANONICAL:
                    raise WeeklyMenuRuntimeUnavailableError(
                        WeeklyMenuRuntimeAvailability(
                            status=FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE,
                            enabled=availability.enabled,
                            allowlist_count=availability.allowlist_count,
                            configuration_valid=availability.configuration_valid,
                            household_ready=True,
                            schema_ready=False,
                        )
                    )
                result = operation(context, store)
        except sqlite3.Error:
            raise WeeklyMenuRuntimeUnavailableError(
                WeeklyMenuRuntimeAvailability(
                    status=FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE,
                    enabled=availability.enabled,
                    allowlist_count=availability.allowlist_count,
                    configuration_valid=availability.configuration_valid,
                    household_ready=True,
                    schema_ready=False,
                )
            ) from None
        self._raise_cleanup_error(resource, WeeklyMenuRuntimeCleanupError("weekly menu runtime cleanup failure"))
        return result

    def get_availability(self, actor_user_id: object) -> WeeklyMenuRuntimeAvailability:
        decision = self._evaluate_gate(actor_user_id)
        if not decision.ready:
            return _availability_from_decision(decision)
        try:
            self._with_store(actor_user_id, lambda _context, _store: None)
        except WeeklyMenuRuntimeUnavailableError as exc:
            return exc.availability
        return WeeklyMenuRuntimeAvailability(
            status=FeatureAvailabilityStatus.READY,
            enabled=True,
            allowlist_count=len(self._config.allowlist),
            configuration_valid=self._config.configuration_valid,
            household_ready=True,
            schema_ready=True,
        )

    def get_weekly_menu_for_week(self, actor_user_id: object, week_start: str) -> WeeklyMenuWeekView | None:
        try:
            def _read(context: HouseholdAuthorizationContext, store: HealBiteWeeklyMenuStore) -> WeeklyMenuWeekView | None:
                series = store.get_weekly_menu_series(context, context.household_id, week_start)
                if series is None:
                    return None
                revisions = store.list_weekly_menu_revisions(context, series.id)
                return WeeklyMenuWeekView(series=series, revisions=revisions)

            return self._with_store(actor_user_id, _read)
        except WeeklyMenuRuntimeUnavailableError:
            raise
        except WeeklyMenuRuntimeCleanupError:
            raise
        except (WeeklyMenuAccessError, WeeklyMenuValidationError):
            raise WeeklyMenuRuntimeStateError("weekly menu read rejected") from None
        except (WeeklyMenuSchemaError, WeeklyMenuStateError, sqlite3.Error):
            raise WeeklyMenuRuntimeStateError("weekly menu runtime failure") from None
        except Exception:
            raise WeeklyMenuRuntimeStateError("weekly menu runtime failure") from None

    def get_weekly_menu_revision(self, actor_user_id: object, revision_id: str) -> WeeklyMenuRevisionView:
        try:
            return self._with_store(
                actor_user_id,
                lambda context, store: store.get_weekly_menu_revision(context, revision_id),
            )
        except WeeklyMenuRuntimeUnavailableError:
            raise
        except WeeklyMenuRuntimeCleanupError:
            raise
        except (WeeklyMenuNotFoundError, WeeklyMenuAccessError):
            raise WeeklyMenuRuntimeNotFoundError("weekly menu revision not found") from None
        except WeeklyMenuValidationError:
            raise WeeklyMenuRuntimeStateError("weekly menu read rejected") from None
        except (WeeklyMenuSchemaError, WeeklyMenuStateError, sqlite3.Error):
            raise WeeklyMenuRuntimeStateError("weekly menu runtime failure") from None
        except Exception:
            raise WeeklyMenuRuntimeStateError("weekly menu runtime failure") from None

    def list_weekly_menu_revisions(self, actor_user_id: object, week_start: str) -> tuple[WeeklyMenuRevision, ...]:
        view = self.get_weekly_menu_for_week(actor_user_id, week_start)
        return tuple() if view is None else view.revisions



def build_weekly_menu_runtime_service(
    *,
    env: dict[str, str] | None = None,
    db_path: str | Path | None = None,
    household_store_factory: HouseholdStoreResourceFactory | None = None,
    weekly_menu_store_factory: WeeklyMenuStoreResourceFactory | None = None,
) -> HealBiteWeeklyMenuRuntimeService:
    return HealBiteWeeklyMenuRuntimeService(
        config=load_feature_gate_config("HEALBITE_WEEKLY_MENU", env),
        db_path=db_path,
        household_store_factory=household_store_factory,
        weekly_menu_store_factory=weekly_menu_store_factory,
    )
