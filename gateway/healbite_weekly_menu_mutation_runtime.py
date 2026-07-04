from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

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
    WeeklyMenuConflictError,
    WeeklyMenuEntryInput,
    WeeklyMenuNotFoundError,
    WeeklyMenuRevisionView,
    WeeklyMenuSchemaError,
    WeeklyMenuStateError,
    WeeklyMenuValidationError,
)
from gateway.healbite_household_schema import HouseholdMemberStatus, HouseholdRole, HouseholdStatus


HouseholdStoreResourceFactory = Callable[[], RuntimeResource[HealBiteHouseholdStore]]
WeeklyMenuStoreResourceFactory = Callable[[], RuntimeResource[HealBiteWeeklyMenuStore]]


class WeeklyMenuMutationStatus(str, Enum):
    SUCCESS = "success"
    DISABLED = "disabled"
    MISCONFIGURED = "misconfigured"
    INVALID_ACTOR = "invalid_actor"
    NOT_ALLOWLISTED = "not_allowlisted"
    HOUSEHOLD_UNAVAILABLE = "household_unavailable"
    SCHEMA_UNAVAILABLE = "schema_unavailable"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    VALIDATION_FAILED = "validation_failed"
    VERSION_CONFLICT = "version_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_STATE = "invalid_state"
    STORAGE_FAILURE = "storage_failure"
    CLEANUP_FAILURE = "cleanup_failure"


@dataclass(frozen=True, slots=True)
class WeeklyMenuMutationResult:
    status: WeeklyMenuMutationStatus
    revision_view: WeeklyMenuRevisionView | None = None
    feature_status: FeatureAvailabilityStatus | None = None

    @property
    def success(self) -> bool:
        return self.status is WeeklyMenuMutationStatus.SUCCESS and self.revision_view is not None


def _gate_failure_result(decision: FeatureGateDecision) -> WeeklyMenuMutationResult:
    status_map = {
        FeatureAvailabilityStatus.DISABLED: WeeklyMenuMutationStatus.DISABLED,
        FeatureAvailabilityStatus.MISCONFIGURED: WeeklyMenuMutationStatus.MISCONFIGURED,
        FeatureAvailabilityStatus.INVALID_ACTOR: WeeklyMenuMutationStatus.INVALID_ACTOR,
        FeatureAvailabilityStatus.NOT_ALLOWLISTED: WeeklyMenuMutationStatus.NOT_ALLOWLISTED,
        FeatureAvailabilityStatus.HOUSEHOLD_UNAVAILABLE: WeeklyMenuMutationStatus.HOUSEHOLD_UNAVAILABLE,
        FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE: WeeklyMenuMutationStatus.SCHEMA_UNAVAILABLE,
    }
    return WeeklyMenuMutationResult(
        status=status_map[decision.status],
        feature_status=decision.status,
    )


class _MutationCleanupError(RuntimeError):
    pass


class HealBiteWeeklyMenuMutationRuntimeService:
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

    def _raise_cleanup_error(self, resource: RuntimeResource[object]) -> None:
        if resource.cleanup_error is not None:
            raise _MutationCleanupError("weekly menu mutation cleanup failure")

    def _resolve_owner_context(
        self,
        actor_user_id: object,
    ) -> tuple[HouseholdAuthorizationContext, FeatureGateDecision]:
        decision = self._evaluate_gate(actor_user_id)
        if not decision.ready:
            raise WeeklyMenuMutationRuntimeUnavailableError(_gate_failure_result(decision))
        assert decision.actor_user_id is not None
        resource = self._household_store_factory()
        try:
            with resource as household_store:
                service = HealBiteHouseholdService(household_store)
                context = service.resolve_existing_actor_household_context(decision.actor_user_id)
        except (HouseholdValidationError, HouseholdNotFoundError, HouseholdAccessError, HouseholdIntegrityError, sqlite3.Error):
            raise WeeklyMenuMutationRuntimeUnavailableError(
                WeeklyMenuMutationResult(
                    status=WeeklyMenuMutationStatus.HOUSEHOLD_UNAVAILABLE,
                    feature_status=FeatureAvailabilityStatus.HOUSEHOLD_UNAVAILABLE,
                )
            ) from None
        self._raise_cleanup_error(resource)
        auth = HouseholdAuthorizationContext.from_household_context(context)
        if (
            auth.member_status is not HouseholdMemberStatus.ACTIVE
            or auth.household_status is not HouseholdStatus.ACTIVE
            or auth.role is not HouseholdRole.OWNER
        ):
            raise WeeklyMenuMutationAccessError(WeeklyMenuMutationResult(status=WeeklyMenuMutationStatus.FORBIDDEN))
        return auth, decision

    def _with_store(
        self,
        actor_user_id: object,
        operation: Callable[[HouseholdAuthorizationContext, HealBiteWeeklyMenuStore], WeeklyMenuRevisionView],
    ) -> WeeklyMenuMutationResult:
        try:
            context, _decision = self._resolve_owner_context(actor_user_id)
            resource = self._weekly_menu_store_factory()
            try:
                with resource as store:
                    if store.schema_state() is not WeeklyMenuSchemaState.CANONICAL:
                        return WeeklyMenuMutationResult(
                            status=WeeklyMenuMutationStatus.SCHEMA_UNAVAILABLE,
                            feature_status=FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE,
                        )
                    revision_view = operation(context, store)
            except sqlite3.Error:
                return WeeklyMenuMutationResult(status=WeeklyMenuMutationStatus.STORAGE_FAILURE)
            self._raise_cleanup_error(resource)
            return WeeklyMenuMutationResult(status=WeeklyMenuMutationStatus.SUCCESS, revision_view=revision_view)
        except WeeklyMenuMutationRuntimeUnavailableError as exc:
            return exc.result
        except WeeklyMenuMutationAccessError as exc:
            return exc.result
        except _MutationCleanupError:
            return WeeklyMenuMutationResult(status=WeeklyMenuMutationStatus.CLEANUP_FAILURE)
        except WeeklyMenuValidationError:
            return WeeklyMenuMutationResult(status=WeeklyMenuMutationStatus.VALIDATION_FAILED)
        except WeeklyMenuNotFoundError:
            return WeeklyMenuMutationResult(status=WeeklyMenuMutationStatus.NOT_FOUND)
        except WeeklyMenuAccessError:
            return WeeklyMenuMutationResult(status=WeeklyMenuMutationStatus.NOT_FOUND)
        except WeeklyMenuConflictError as exc:
            message = str(exc).lower()
            if "idempotency key replayed with different payload" in message:
                return WeeklyMenuMutationResult(status=WeeklyMenuMutationStatus.IDEMPOTENCY_CONFLICT)
            return WeeklyMenuMutationResult(status=WeeklyMenuMutationStatus.VERSION_CONFLICT)
        except (WeeklyMenuStateError, WeeklyMenuSchemaError):
            return WeeklyMenuMutationResult(status=WeeklyMenuMutationStatus.INVALID_STATE)
        except Exception:
            return WeeklyMenuMutationResult(status=WeeklyMenuMutationStatus.STORAGE_FAILURE)

    def create_draft_for_week(
        self,
        actor_user_id: object,
        week_start: str,
        *,
        expected_series_version: int | None,
        idempotency_key: str,
    ) -> WeeklyMenuMutationResult:
        def _mutate(context: HouseholdAuthorizationContext, store: HealBiteWeeklyMenuStore) -> WeeklyMenuRevisionView:
            series = store.get_weekly_menu_series(context, context.household_id, week_start)
            if series is None:
                if expected_series_version is not None:
                    raise WeeklyMenuConflictError("weekly menu series version mismatch")
                series = store.create_or_get_weekly_menu_series(context, context.household_id, week_start)
            elif expected_series_version is None:
                raise WeeklyMenuConflictError("weekly menu series version mismatch")
            return store.create_draft_revision(
                context,
                series.id,
                expected_series_version=series.version if expected_series_version is None else expected_series_version,
                idempotency_key=idempotency_key,
            )

        return self._with_store(actor_user_id, _mutate)

    def replace_draft_entries(
        self,
        actor_user_id: object,
        revision_id: str,
        entries: list[WeeklyMenuEntryInput] | tuple[WeeklyMenuEntryInput, ...],
        *,
        expected_revision_version: int,
        idempotency_key: str,
    ) -> WeeklyMenuMutationResult:
        return self._with_store(
            actor_user_id,
            lambda context, store: store.replace_draft_entries(
                context,
                revision_id,
                entries,
                expected_revision_version=expected_revision_version,
                idempotency_key=idempotency_key,
            ),
        )

    def publish_draft(
        self,
        actor_user_id: object,
        revision_id: str,
        *,
        expected_series_version: int,
        expected_revision_version: int,
        idempotency_key: str,
    ) -> WeeklyMenuMutationResult:
        return self._with_store(
            actor_user_id,
            lambda context, store: store.publish_weekly_menu_revision(
                context,
                revision_id,
                expected_series_version=expected_series_version,
                expected_revision_version=expected_revision_version,
                idempotency_key=idempotency_key,
            ),
        )

    def archive_revision(
        self,
        actor_user_id: object,
        revision_id: str,
        *,
        expected_series_version: int,
        expected_revision_version: int,
        idempotency_key: str,
    ) -> WeeklyMenuMutationResult:
        return self._with_store(
            actor_user_id,
            lambda context, store: store.archive_weekly_menu_revision(
                context,
                revision_id,
                expected_series_version=expected_series_version,
                expected_revision_version=expected_revision_version,
                idempotency_key=idempotency_key,
            ),
        )


class WeeklyMenuMutationRuntimeUnavailableError(RuntimeError):
    def __init__(self, result: WeeklyMenuMutationResult) -> None:
        super().__init__("weekly menu mutation runtime unavailable")
        self.result = result


class WeeklyMenuMutationAccessError(RuntimeError):
    def __init__(self, result: WeeklyMenuMutationResult) -> None:
        super().__init__("weekly menu mutation access denied")
        self.result = result
