from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence, TypeVar

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
from gateway.healbite_weekly_menu_schema import (
    WeeklyMenuSchemaState,
    require_monday_week_start,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    HouseholdAuthorizationContext,
    WeeklyMenuAccessError,
    WeeklyMenuNotFoundError,
    WeeklyMenuRevisionStatus,
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


_FRIDGE_MENU_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_FRIDGE_MENU_MEAL_TYPES = ("breakfast", "lunch", "dinner")
_FRIDGE_MENU_SYSTEM_PROMPT = (
    "You generate a seven-day meal plan from the user's confirmed refrigerator inventory. "
    "Return exactly one valid JSON object and nothing else: no markdown, code fences, comments, or prose. "
    "The object must contain exactly two top-level keys: days and missing_ingredients_to_buy. "
    "days must contain exactly seven objects in canonical order monday through sunday. Each day object "
    "must contain exactly day and meals. meals must contain exactly breakfast, lunch, and dinner once each. "
    "Each meal must contain exactly meal_type, title, and ingredients. Each ingredient must contain exactly "
    "name, quantity, unit, and is_in_inventory, where is_in_inventory is a JSON boolean. "
    "missing_ingredients_to_buy must be an array of unique objects containing exactly name, quantity, and unit. "
    "It must contain every ingredient whose is_in_inventory value is false and no ingredient whose value is true. "
    "Use inventory ingredients whenever practical, never claim an absent ingredient is in inventory, and obey "
    "dietary restrictions. Use the requested locale for human-readable values."
)
_VISION_INGREDIENT_SEPARATOR_RE = re.compile(r"[\n,;]+")
_VISION_INGREDIENT_PREFIX_RE = re.compile(r"^\s*(?:(?:[-*\u2022]+)|(?:\d+[.)]))\s*")
_MAX_PROMPT_INGREDIENTS = 200
_MAX_PROMPT_ITEM_LENGTH = 200
_MAX_VISION_TEXT_LENGTH = 20_000


@dataclass(frozen=True, slots=True)
class WeeklyMenuPromptBundle:
    system_prompt: str
    user_prompt: str


class WeeklyMenuPromptValidationError(ValueError):
    pass


def _normalize_prompt_values(
    values: Sequence[str],
    *,
    label: str,
    maximum: int,
) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise WeeklyMenuPromptValidationError(f"{label} must be a sequence")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = " ".join(str(raw or "").split())
        if not value or len(value) > _MAX_PROMPT_ITEM_LENGTH:
            raise WeeklyMenuPromptValidationError(f"invalid {label} item")
        key = value.casefold()
        if key in seen:
            continue
        normalized.append(value)
        seen.add(key)
        if len(normalized) > maximum:
            raise WeeklyMenuPromptValidationError(f"too many {label} items")
    return tuple(normalized)


def build_fridge_weekly_menu_prompts(
    inventory_ingredients: Sequence[str],
    *,
    week_start: str,
    dietary_restrictions: Sequence[str] = (),
    locale: str = "ru-RU",
) -> WeeklyMenuPromptBundle:
    """Build a strict, cache-stable LLM prompt pair for fridge-first menus."""

    try:
        canonical_week_start = require_monday_week_start(
            str(week_start).strip()
        )
    except (TypeError, ValueError) as exc:
        raise WeeklyMenuPromptValidationError("invalid week_start") from exc
    normalized_locale = str(locale or "").strip()
    if not normalized_locale or len(normalized_locale) > 32:
        raise WeeklyMenuPromptValidationError("invalid locale")
    inventory = _normalize_prompt_values(
        inventory_ingredients,
        label="inventory",
        maximum=_MAX_PROMPT_INGREDIENTS,
    )
    restrictions = _normalize_prompt_values(
        dietary_restrictions,
        label="dietary restriction",
        maximum=32,
    )
    request = {
        "dietary_restrictions": list(restrictions),
        "inventory_ingredients": list(inventory),
        "locale": normalized_locale,
        "meal_types": list(_FRIDGE_MENU_MEAL_TYPES),
        "week_start": canonical_week_start,
        "weekdays": list(_FRIDGE_MENU_WEEKDAYS),
    }
    return WeeklyMenuPromptBundle(
        system_prompt=_FRIDGE_MENU_SYSTEM_PROMPT,
        user_prompt=json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def parse_fridge_vision_ingredients(vision_text: str) -> list[str]:
    """Parse a Vision model's draft text into normalized ingredient names."""

    if not isinstance(vision_text, str):
        raise WeeklyMenuPromptValidationError("vision text must be a string")
    if len(vision_text) > _MAX_VISION_TEXT_LENGTH:
        raise WeeklyMenuPromptValidationError("vision text is too long")
    ingredients: list[str] = []
    seen: set[str] = set()
    for chunk in _VISION_INGREDIENT_SEPARATOR_RE.split(vision_text):
        value = _VISION_INGREDIENT_PREFIX_RE.sub("", chunk).strip()
        value = " ".join(value.split())
        if not value:
            continue
        if len(value) > _MAX_PROMPT_ITEM_LENGTH:
            raise WeeklyMenuPromptValidationError("invalid vision ingredient")
        key = value.casefold()
        if key in seen:
            continue
        ingredients.append(value)
        seen.add(key)
        if len(ingredients) > _MAX_PROMPT_INGREDIENTS:
            raise WeeklyMenuPromptValidationError("too many vision ingredients")
    return ingredients




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

    def get_active_published_weekly_menu_for_week(
        self,
        actor_user_id: object,
        week_start: str,
    ) -> WeeklyMenuRevisionView | None:
        try:
            def _read(context: HouseholdAuthorizationContext, store: HealBiteWeeklyMenuStore) -> WeeklyMenuRevisionView | None:
                series = store.get_weekly_menu_series(context, context.household_id, week_start)
                if series is None:
                    return None
                revisions = store.list_weekly_menu_revisions(context, series.id)
                published: WeeklyMenuRevision | None = None
                for revision in revisions:
                    if revision.status is not WeeklyMenuRevisionStatus.PUBLISHED:
                        continue
                    if published is not None:
                        raise WeeklyMenuStateError("multiple active published revisions")
                    published = revision
                if published is None:
                    return None
                return store.get_weekly_menu_revision(context, published.id)

            return self._with_store(actor_user_id, _read)
        except WeeklyMenuRuntimeUnavailableError:
            raise
        except WeeklyMenuRuntimeCleanupError:
            raise
        except (WeeklyMenuAccessError, WeeklyMenuValidationError):
            raise WeeklyMenuRuntimeStateError("weekly menu read rejected") from None
        except (WeeklyMenuNotFoundError, WeeklyMenuSchemaError, WeeklyMenuStateError, sqlite3.Error):
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
