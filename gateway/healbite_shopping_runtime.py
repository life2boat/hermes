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
from gateway.healbite_shopping import (
    HealBiteShoppingStore,
    ManualShoppingItemInput,
    ShoppingAccessError,
    ShoppingConflictError,
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


class ShoppingRuntimeConflictError(ShoppingRuntimeStateError):
    pass


class ShoppingRuntimeSourceError(ShoppingRuntimeStateError):
    pass


class ShoppingRuntimeCleanupError(ShoppingRuntimeStateError):
    pass


HouseholdStoreResourceFactory = Callable[[], RuntimeResource[HealBiteHouseholdStore]]
ShoppingStoreResourceFactory = Callable[[], RuntimeResource[HealBiteShoppingStore]]
T = TypeVar("T")


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
        household_store_factory: HouseholdStoreResourceFactory | None = None,
        shopping_store_factory: ShoppingStoreResourceFactory | None = None,
    ) -> None:
        self._config = config if config is not None else load_feature_gate_config("HEALBITE_SHOPPING_LIST")
        self._db_path = resolve_healbite_db_path(db_path)
        self._household_store_factory = household_store_factory or self._default_household_store_factory
        self._shopping_store_factory = shopping_store_factory or self._default_shopping_store_factory

    def _default_household_store_factory(self) -> RuntimeResource[HealBiteHouseholdStore]:
        return borrowed_runtime_resource(HealBiteHouseholdStore(db_path=self._db_path, ensure_schema_on_init=False))

    def _default_shopping_store_factory(self) -> RuntimeResource[HealBiteShoppingStore]:
        return borrowed_runtime_resource(HealBiteShoppingStore(db_path=self._db_path))

    def _evaluate_gate(self, actor_user_id: object) -> FeatureGateDecision:
        return evaluate_feature_gate(self._config, actor_user_id)

    def _resolve_authorization_context(self, actor_user_id: object) -> tuple[HouseholdAuthorizationContext, ShoppingRuntimeAvailability]:
        decision = self._evaluate_gate(actor_user_id)
        if not decision.ready:
            raise ShoppingRuntimeUnavailableError(_availability_from_decision(decision))
        assert decision.actor_user_id is not None
        resource = self._household_store_factory()
        try:
            with resource as household_store:
                service = HealBiteHouseholdService(household_store)
                context = service.resolve_existing_actor_household_context(decision.actor_user_id)
        except (HouseholdValidationError, HouseholdNotFoundError, HouseholdAccessError, HouseholdIntegrityError, sqlite3.Error):
            raise ShoppingRuntimeUnavailableError(
                _availability_from_decision(
                    decision,
                    status=FeatureAvailabilityStatus.HOUSEHOLD_UNAVAILABLE,
                )
            ) from None
        self._raise_cleanup_error(resource, ShoppingRuntimeCleanupError("shopping runtime cleanup failure"))
        return (
            HouseholdAuthorizationContext.from_household_context(context),
            _availability_from_decision(decision, household_ready=True),
        )

    def _raise_cleanup_error(self, resource: RuntimeResource[object], error: ShoppingRuntimeCleanupError) -> None:
        if resource.cleanup_error is not None:
            raise error from None

    def _with_store(self, actor_user_id: object, operation: Callable[[HouseholdAuthorizationContext, HealBiteShoppingStore], T]) -> T:
        context, availability = self._resolve_authorization_context(actor_user_id)
        resource = self._shopping_store_factory()
        try:
            with resource as store:
                state = store.schema_state()
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
                result = operation(context, store)
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
        self._raise_cleanup_error(resource, ShoppingRuntimeCleanupError("shopping runtime cleanup failure"))
        return result

    def get_availability(self, actor_user_id: object) -> ShoppingRuntimeAvailability:
        decision = self._evaluate_gate(actor_user_id)
        if not decision.ready:
            return _availability_from_decision(decision)
        try:
            self._with_store(actor_user_id, lambda _context, _store: None)
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
        try:
            return self._with_store(
                actor_user_id,
                lambda context, store: store.get_shopping_list(context, shopping_list_id),
            )
        except ShoppingRuntimeUnavailableError:
            raise
        except ShoppingRuntimeCleanupError:
            raise
        except (ShoppingNotFoundError, ShoppingAccessError):
            raise ShoppingRuntimeNotFoundError("shopping list not found") from None
        except ShoppingValidationError:
            raise ShoppingRuntimeStateError("shopping read rejected") from None
        except (ShoppingSchemaError, ShoppingStateError, sqlite3.Error):
            raise ShoppingRuntimeStateError("shopping runtime failure") from None
        except Exception:
            raise ShoppingRuntimeStateError("shopping runtime failure") from None

    def list_shopping_lists(
        self,
        actor_user_id: object,
        filters: ShoppingListFilters | None = None,
    ) -> tuple[ShoppingList, ...]:
        selected_filters = filters or ShoppingListFilters()
        try:
            return self._with_store(
                actor_user_id,
                lambda context, store: store.list_shopping_lists(
                    context,
                    context.household_id,
                    week_start=selected_filters.week_start,
                ),
            )
        except ShoppingRuntimeUnavailableError:
            raise
        except ShoppingRuntimeCleanupError:
            raise
        except (ShoppingAccessError, ShoppingValidationError):
            raise ShoppingRuntimeStateError("shopping read rejected") from None
        except (ShoppingSchemaError, ShoppingStateError, sqlite3.Error):
            raise ShoppingRuntimeStateError("shopping runtime failure") from None
        except Exception:
            raise ShoppingRuntimeStateError("shopping runtime failure") from None

    def list_shopping_items(self, actor_user_id: object, shopping_list_id: str) -> tuple[ShoppingItem, ...]:
        return self.get_shopping_list(actor_user_id, shopping_list_id).items

    def get_current_shopping_list(self, actor_user_id: object, week_key: str) -> ShoppingListView | None:
        return self._run_public_operation(
            actor_user_id,
            lambda context, store: store.get_current_shopping_list(
                context,
                context.household_id,
                week_start=week_key,
            ),
            not_found_message="shopping list not found",
        )

    def add_manual_shopping_item(
        self,
        actor_user_id: object,
        week_key: str,
        name: str,
        quantity: str | None,
        unit: str,
        idempotency_key: str,
        expected_list_version: int,
    ) -> ShoppingListView:
        def operation(context: HouseholdAuthorizationContext, store: HealBiteShoppingStore) -> ShoppingListView:
            current = store.get_current_shopping_list(context, context.household_id, week_start=week_key)
            if current is None:
                raise ShoppingNotFoundError("shopping list not found")
            return store.add_manual_item(
                context,
                current.shopping_list.id,
                ManualShoppingItemInput(
                    display_name=name,
                    quantity_value=quantity,
                    quantity_unit_normalized=unit,
                ),
                expected_list_version=expected_list_version,
                idempotency_key=idempotency_key,
            )

        return self._run_public_operation(actor_user_id, operation, not_found_message="shopping list not found")

    def set_shopping_item_checked(
        self,
        actor_user_id: object,
        item_reference: str,
        checked: bool,
        idempotency_key: str,
        expected_item_version: int,
    ) -> ShoppingListView:
        return self._run_public_operation(
            actor_user_id,
            lambda context, store: store.set_item_checked(
                context,
                item_reference,
                checked,
                expected_item_version=expected_item_version,
                idempotency_key=idempotency_key,
            ),
            not_found_message="shopping item not found",
        )

    def delete_shopping_item(
        self,
        actor_user_id: object,
        item_reference: str,
        idempotency_key: str,
        expected_item_version: int,
    ) -> ShoppingListView:
        return self._run_public_operation(
            actor_user_id,
            lambda context, store: store.delete_item(
                context,
                item_reference,
                expected_item_version=expected_item_version,
                idempotency_key=idempotency_key,
            ),
            not_found_message="shopping item not found",
        )

    def clear_shopping_list(
        self,
        actor_user_id: object,
        week_key: str,
        clear_mode: str,
        idempotency_key: str,
        expected_list_version: int,
    ) -> ShoppingListView:
        def operation(context: HouseholdAuthorizationContext, store: HealBiteShoppingStore) -> ShoppingListView:
            current = store.get_current_shopping_list(context, context.household_id, week_start=week_key)
            if current is None:
                raise ShoppingNotFoundError("shopping list not found")
            return store.clear_shopping_list(
                context,
                current.shopping_list.id,
                clear_mode=clear_mode,
                expected_list_version=expected_list_version,
                idempotency_key=idempotency_key,
            )

        return self._run_public_operation(actor_user_id, operation, not_found_message="shopping list not found")

    def generate_shopping_list_from_weekly_menu(
        self,
        actor_user_id: object,
        week_key: str,
        idempotency_key: str,
        expected_list_version: int | None,
    ) -> ShoppingListView:
        """Derive one actor-scoped list from the published menu for a week."""
        try:
            return self._with_store(
                actor_user_id,
                lambda context, store: store.generate_shopping_list_from_weekly_menu(
                    context,
                    week_key,
                    expected_list_version=expected_list_version,
                    idempotency_key=idempotency_key,
                ),
            )
        except (ShoppingRuntimeUnavailableError, ShoppingRuntimeCleanupError):
            raise
        except (ShoppingNotFoundError, ShoppingAccessError):
            raise ShoppingRuntimeNotFoundError("published weekly menu not found") from None
        except ShoppingConflictError:
            raise ShoppingRuntimeConflictError("shopping derivation conflict") from None
        except (ShoppingValidationError, ShoppingStateError):
            raise ShoppingRuntimeSourceError("shopping source rejected") from None
        except (ShoppingSchemaError, sqlite3.Error):
            raise ShoppingRuntimeStateError("shopping runtime failure") from None
        except Exception:
            raise ShoppingRuntimeStateError("shopping runtime failure") from None

    def _run_public_operation(
        self,
        actor_user_id: object,
        operation: Callable[[HouseholdAuthorizationContext, HealBiteShoppingStore], T],
        *,
        not_found_message: str,
    ) -> T:
        try:
            return self._with_store(actor_user_id, operation)
        except (ShoppingRuntimeUnavailableError, ShoppingRuntimeCleanupError):
            raise
        except (ShoppingNotFoundError, ShoppingAccessError):
            raise ShoppingRuntimeNotFoundError(not_found_message) from None
        except (ShoppingConflictError, ShoppingValidationError):
            raise ShoppingRuntimeStateError("shopping mutation rejected") from None
        except (ShoppingSchemaError, ShoppingStateError, sqlite3.Error):
            raise ShoppingRuntimeStateError("shopping runtime failure") from None
        except Exception:
            raise ShoppingRuntimeStateError("shopping runtime failure") from None


def build_shopping_runtime_service(
    *,
    env: dict[str, str] | None = None,
    db_path: str | Path | None = None,
    household_store_factory: HouseholdStoreResourceFactory | None = None,
    shopping_store_factory: ShoppingStoreResourceFactory | None = None,
) -> HealBiteShoppingRuntimeService:
    return HealBiteShoppingRuntimeService(
        config=load_feature_gate_config("HEALBITE_SHOPPING_LIST", env),
        db_path=db_path,
        household_store_factory=household_store_factory,
        shopping_store_factory=shopping_store_factory,
    )
