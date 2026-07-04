from __future__ import annotations

import sqlite3
from dataclasses import dataclass
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
from gateway.healbite_shopping import (
    HealBiteShoppingStore,
    ShoppingAccessError,
    ShoppingItem,
    ShoppingList,
    ShoppingListView,
    ShoppingNotFoundError,
    ShoppingSchemaError,
    ShoppingStateError,
    ShoppingValidationError,
)
from gateway.healbite_shopping_schema import ShoppingSchemaState
from gateway.healbite_weekly_menus import HouseholdAuthorizationContext


@dataclass(frozen=True, slots=True)
class ShoppingRuntimeAvailability:
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
class ShoppingListFilters:
    week_start: str | None = None


class ShoppingRuntimeError(Exception):
    pass


class ShoppingRuntimeUnavailableError(ShoppingRuntimeError):
    def __init__(self, availability: ShoppingRuntimeAvailability) -> None:
        super().__init__("shopping runtime unavailable")
        self.availability = availability


class ShoppingRuntimeNotFoundError(ShoppingRuntimeError):
    pass


class ShoppingRuntimeStateError(ShoppingRuntimeError):
    pass


HouseholdStoreFactory = Callable[[], HealBiteHouseholdStore]
ShoppingStoreFactory = Callable[[], HealBiteShoppingStore]


def _availability_from_decision(
    decision: FeatureGateDecision,
    *,
    household_ready: bool = False,
    schema_ready: bool = False,
    status: FeatureAvailabilityStatus | None = None,
) -> ShoppingRuntimeAvailability:
    return ShoppingRuntimeAvailability(
        status=status or decision.status,
        enabled=decision.enabled,
        allowlist_count=decision.allowlist_count,
        configuration_valid=decision.configuration_valid,
        household_ready=household_ready,
        schema_ready=schema_ready,
    )


class HealBiteShoppingRuntimeService:
    def __init__(
        self,
        *,
        config: FeatureGateConfig | None = None,
        db_path: str | Path | None = None,
        household_store_factory: HouseholdStoreFactory | None = None,
        shopping_store_factory: ShoppingStoreFactory | None = None,
    ) -> None:
        self._config = config if config is not None else load_feature_gate_config("HEALBITE_SHOPPING_LIST")
        self._db_path = resolve_healbite_db_path(db_path)
        self._household_store_factory = household_store_factory or self._default_household_store_factory
        self._shopping_store_factory = shopping_store_factory or self._default_shopping_store_factory

    def _default_household_store_factory(self) -> HealBiteHouseholdStore:
        return HealBiteHouseholdStore(db_path=self._db_path, ensure_schema_on_init=False)

    def _default_shopping_store_factory(self) -> HealBiteShoppingStore:
        return HealBiteShoppingStore(db_path=self._db_path)

    def _evaluate_gate(self, actor_user_id: object) -> FeatureGateDecision:
        return evaluate_feature_gate(self._config, actor_user_id)

    def _resolve_authorization_context(self, actor_user_id: object) -> tuple[HouseholdAuthorizationContext, ShoppingRuntimeAvailability]:
        decision = self._evaluate_gate(actor_user_id)
        if not decision.ready:
            raise ShoppingRuntimeUnavailableError(_availability_from_decision(decision))
        assert decision.actor_user_id is not None
        try:
            service = HealBiteHouseholdService(self._household_store_factory())
            context = service.resolve_existing_actor_household_context(decision.actor_user_id)
        except (HouseholdValidationError, HouseholdNotFoundError, HouseholdAccessError, HouseholdIntegrityError, sqlite3.Error):
            raise ShoppingRuntimeUnavailableError(
                _availability_from_decision(
                    decision,
                    status=FeatureAvailabilityStatus.HOUSEHOLD_UNAVAILABLE,
                )
            ) from None
        return (
            HouseholdAuthorizationContext.from_household_context(context),
            _availability_from_decision(decision, household_ready=True),
        )

    def _resolve_store(self, actor_user_id: object) -> tuple[HouseholdAuthorizationContext, HealBiteShoppingStore]:
        context, availability = self._resolve_authorization_context(actor_user_id)
        try:
            store = self._shopping_store_factory()
            state = store.schema_state()
        except sqlite3.Error:
            raise ShoppingRuntimeUnavailableError(
                ShoppingRuntimeAvailability(
                    status=FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE,
                    enabled=availability.enabled,
                    allowlist_count=availability.allowlist_count,
                    configuration_valid=availability.configuration_valid,
                    household_ready=True,
                    schema_ready=False,
                )
            ) from None
        if state is not ShoppingSchemaState.CANONICAL:
            raise ShoppingRuntimeUnavailableError(
                ShoppingRuntimeAvailability(
                    status=FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE,
                    enabled=availability.enabled,
                    allowlist_count=availability.allowlist_count,
                    configuration_valid=availability.configuration_valid,
                    household_ready=True,
                    schema_ready=False,
                )
            )
        return context, store

    def get_availability(self, actor_user_id: object) -> ShoppingRuntimeAvailability:
        decision = self._evaluate_gate(actor_user_id)
        if not decision.ready:
            return _availability_from_decision(decision)
        try:
            self._resolve_store(actor_user_id)
        except ShoppingRuntimeUnavailableError as exc:
            return exc.availability
        return ShoppingRuntimeAvailability(
            status=FeatureAvailabilityStatus.READY,
            enabled=True,
            allowlist_count=len(self._config.allowlist),
            configuration_valid=self._config.configuration_valid,
            household_ready=True,
            schema_ready=True,
        )

    def get_shopping_list(self, actor_user_id: object, shopping_list_id: str) -> ShoppingListView:
        context, store = self._resolve_store(actor_user_id)
        try:
            return store.get_shopping_list(context, shopping_list_id)
        except ShoppingNotFoundError:
            raise ShoppingRuntimeNotFoundError("shopping list not found") from None
        except (ShoppingAccessError, ShoppingValidationError):
            raise ShoppingRuntimeStateError("shopping read rejected") from None
        except (ShoppingSchemaError, ShoppingStateError, sqlite3.Error):
            raise ShoppingRuntimeStateError("shopping runtime failure") from None

    def list_shopping_lists(
        self,
        actor_user_id: object,
        filters: ShoppingListFilters | None = None,
    ) -> tuple[ShoppingList, ...]:
        context, store = self._resolve_store(actor_user_id)
        selected_filters = filters or ShoppingListFilters()
        try:
            return store.list_shopping_lists(
                context,
                context.household_id,
                week_start=selected_filters.week_start,
            )
        except (ShoppingAccessError, ShoppingValidationError):
            raise ShoppingRuntimeStateError("shopping read rejected") from None
        except (ShoppingSchemaError, ShoppingStateError, sqlite3.Error):
            raise ShoppingRuntimeStateError("shopping runtime failure") from None

    def list_shopping_items(self, actor_user_id: object, shopping_list_id: str) -> tuple[ShoppingItem, ...]:
        return self.get_shopping_list(actor_user_id, shopping_list_id).items


def build_shopping_runtime_service(
    *,
    env: dict[str, str] | None = None,
    db_path: str | Path | None = None,
    household_store_factory: HouseholdStoreFactory | None = None,
    shopping_store_factory: ShoppingStoreFactory | None = None,
) -> HealBiteShoppingRuntimeService:
    return HealBiteShoppingRuntimeService(
        config=load_feature_gate_config("HEALBITE_SHOPPING_LIST", env),
        db_path=db_path,
        household_store_factory=household_store_factory,
        shopping_store_factory=shopping_store_factory,
    )
