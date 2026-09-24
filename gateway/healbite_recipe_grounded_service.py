from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from gateway.healbite_feature_gates import (
    FeatureAvailabilityStatus,
    FeatureGateConfig,
    evaluate_feature_gate,
    load_feature_gate_config,
)
from gateway.healbite_household_schema import HouseholdRole
from gateway.healbite_households import HealBiteHouseholdService
from gateway.healbite_inventory import (
    HealBiteInventoryStore,
    InventoryOwnerScope,
    InventoryStatus,
)
from gateway.healbite_recipe_catalog_domain import (
    MealType,
    TargetRightsScope,
    load_target_rights_scope,
    normalize_ingredient_id,
    normalize_unit,
)
from gateway.healbite_recipe_catalog_store import (
    CatalogIntegrityError,
    CatalogNotFoundError,
    CatalogSchemaError,
    HealBiteRecipeCatalogStore,
)
from gateway.healbite_recipe_grounded_planner import (
    PlannedMealSlot,
    PlannerGroundingError,
    RecipeGroundedPlan,
    RecipeGroundedWeeklyPlanner,
)
from gateway.healbite_recipe_grounding_validator import (
    GroundingValidationResult,
    RecipeGroundingValidator,
    ValidatedMealEntry,
)
from gateway.healbite_recipe_retrieval import RecipeCandidate, RecipeRetriever
from gateway.healbite_recipe_servings_shopping import (
    DerivedShoppingItem,
    convert_unit_quantity,
    derive_shopping_list_from_recipes,
)
from gateway.healbite_weekly_menu_schema import (
    WeeklyMenuEntryOrigin,
    new_weekly_menu_entry_id,
    week_dates,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    HouseholdAuthorizationContext,
    WeeklyMenuConflictError,
    WeeklyMenuEntryInput,
    WeeklyMenuEntryRecipeRef,
    WeeklyMenuIngredientInput,
    WeeklyMenuRevisionStatus,
    WeeklyMenuRevisionView,
)

logger = logging.getLogger(__name__)


class RecipeGroundedStatus:
    SUCCESS = "success"
    DISABLED = "disabled"
    NOT_ALLOWLISTED = "not_allowlisted"
    CATALOG_UNAVAILABLE = "catalog_unavailable"
    INSUFFICIENT_GROUNDED_RECIPES = "insufficient_grounded_recipes"
    PLANNER_FAILED = "planner_failed"
    GROUNDING_FAILED = "grounding_failed"
    HOUSEHOLD_UNAVAILABLE = "household_unavailable"
    STORAGE_FAILURE = "storage_failure"


@dataclass(frozen=True, slots=True)
class RecipeGroundedGenerationResult:
    status: str
    revision_view: WeeklyMenuRevisionView | None = None
    grounding_result: GroundingValidationResult | None = None
    shopping_items: tuple[DerivedShoppingItem, ...] = ()
    error_message: str | None = None

    @property
    def success(self) -> bool:
        return self.status == RecipeGroundedStatus.SUCCESS and self.revision_view is not None


def _normalize_weekly_menu_ingredient_unit(raw_unit: str) -> str:
    canon = normalize_unit(raw_unit)
    if canon in ("g", "kg", "ml", "l", "piece", "package", "unitless"):
        return canon
    return "unitless"


def _normalize_weekly_menu_ingredient_qty(qty: Decimal | None) -> str:
    if qty is not None and qty > Decimal("0"):
        return str(qty)
    return "1"


class HealBiteRecipeGroundedService:
    def __init__(
        self,
        *,
        catalog_store_factory: Callable[[], HealBiteRecipeCatalogStore] | None = None,
        catalog_path: str | None = None,
        weekly_menu_store: HealBiteWeeklyMenuStore,
        household_service: HealBiteHouseholdService,
        inventory_store: HealBiteInventoryStore | None = None,
        planner: RecipeGroundedWeeklyPlanner | None = None,
        config: FeatureGateConfig | None = None,
        target_rights_scope: str | Sequence[str] | TargetRightsScope | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._catalog_factory = catalog_store_factory
        self._catalog_path = catalog_path
        self._weekly_menu_store = weekly_menu_store
        self._household_service = household_service
        self._inventory_store = inventory_store
        self._planner = planner or RecipeGroundedWeeklyPlanner()
        self._target_rights_scope = load_target_rights_scope(target_rights_scope, env=env)
        if config is not None:
            self._config = config
        else:
            self._config = load_feature_gate_config("HEALBITE_RECIPE_GROUNDED_MENU", env=env)

    def _get_catalog(self) -> HealBiteRecipeCatalogStore:
        if self._catalog_factory:
            return self._catalog_factory()
        if not self._catalog_path:
            raise CatalogNotFoundError("Recipe catalog path not configured")
        return HealBiteRecipeCatalogStore(
            self._catalog_path,
            read_only=True,
            validate_hash=True,
            target_rights_scope=self._target_rights_scope,
        )

    def generate_menu(
        self,
        actor_user_id: int,
        *,
        week_start: str,
        authors: Sequence[str],
        target_servings: Decimal = Decimal("2"),
        dietary_constraints: Sequence[str] | None = None,
        excluded_ingredients: Sequence[str] | None = None,
        idempotency_key: str | None = None,
        expected_series_version: int | None = None,
    ) -> RecipeGroundedGenerationResult:
        # 1. Feature gate check
        decision = evaluate_feature_gate(self._config, actor_user_id)
        if not decision.ready:
            return RecipeGroundedGenerationResult(
                status=decision.status.value,
                error_message=f"Feature gate {decision.status.value}",
            )

        # 2. Household check
        try:
            raw_context = self._household_service.resolve_existing_actor_household_context(actor_user_id)
            if raw_context.role not in (HouseholdRole.OWNER, HouseholdRole.ADULT_MEMBER):
                return RecipeGroundedGenerationResult(
                    status=RecipeGroundedStatus.HOUSEHOLD_UNAVAILABLE,
                    error_message="Household access denied",
                )
            context = HouseholdAuthorizationContext.from_household_context(raw_context)
        except Exception as exc:
            return RecipeGroundedGenerationResult(
                status=RecipeGroundedStatus.HOUSEHOLD_UNAVAILABLE,
                error_message=str(exc),
            )

        # 3. Open immutable catalog
        try:
            catalog = self._get_catalog()
        except (CatalogNotFoundError, CatalogSchemaError, CatalogIntegrityError) as exc:
            logger.error("[RecipeGroundedService] Catalog error: %s", exc)
            return RecipeGroundedGenerationResult(
                status=RecipeGroundedStatus.CATALOG_UNAVAILABLE,
                error_message=f"Catalog unavailable: {exc}",
            )

        # 4. Read confirmed inventory if available
        available_inv: dict[str, Decimal | None] = {}
        confirmed_inv_tuples: dict[str, tuple[Decimal | None, str]] = {}
        if self._inventory_store:
            try:
                scope = InventoryOwnerScope(household_id=context.household_id)
                snapshot_view = self._inventory_store.get_latest_confirmed_snapshot(scope)
                if snapshot_view and snapshot_view.snapshot.status is InventoryStatus.CONFIRMED:
                    for item in snapshot_view.items:
                        name_to_normalize = (
                            getattr(item, "display_name", None)
                            or getattr(item, "normalized_name", "")
                            or ""
                        ).strip()
                        ing_id = normalize_ingredient_id(name_to_normalize)

                        raw_qty = getattr(item, "quantity_value", None)
                        parsed_qty: Decimal | None = None
                        if raw_qty is not None and str(raw_qty).strip():
                            try:
                                parsed_qty = Decimal(str(raw_qty).strip())
                            except Exception:
                                parsed_qty = None

                        raw_unit = getattr(item, "unit", "unknown")
                        unit_val = raw_unit.value if hasattr(raw_unit, "value") else str(raw_unit)
                        canonical_unit = normalize_unit(unit_val)

                        # Track available ingredients for retrieval ranking
                        if parsed_qty is not None and parsed_qty > Decimal("0"):
                            if ing_id in available_inv and available_inv[ing_id] is not None:
                                available_inv[ing_id] = available_inv[ing_id] + parsed_qty
                            else:
                                available_inv[ing_id] = parsed_qty
                        elif ing_id not in available_inv:
                            # Preserve unknown quantity conservatively
                            available_inv[ing_id] = None

                        # Track confirmed inventory tuples for shopping subtraction
                        if ing_id in confirmed_inv_tuples:
                            prev_qty, prev_unit = confirmed_inv_tuples[ing_id]
                            if prev_qty is not None and parsed_qty is not None:
                                converted = convert_unit_quantity(parsed_qty, canonical_unit, prev_unit)
                                if converted is not None:
                                    confirmed_inv_tuples[ing_id] = (prev_qty + converted, prev_unit)
                            elif prev_qty is None and parsed_qty is not None:
                                confirmed_inv_tuples[ing_id] = (parsed_qty, canonical_unit)
                        else:
                            confirmed_inv_tuples[ing_id] = (parsed_qty, canonical_unit)
            except sqlite3.Error as exc:
                logger.warning("[RecipeGroundedService] Inventory lookup failed: %s", exc)

        # 5. Retrieve candidates
        retriever = RecipeRetriever(catalog, target_rights_scope=self._target_rights_scope)
        slot_candidates: dict[MealType, list[RecipeCandidate]] = {}

        logger.info(
            "[RecipeGroundedService][recipe_retrieval_started] actor=%s week=%s authors=%s",
            actor_user_id,
            week_start,
            authors,
        )

        for slot in (MealType.BREAKFAST, MealType.LUNCH, MealType.DINNER):
            cands = retriever.retrieve_candidates_for_slot(
                meal_slot=slot,
                authors=authors,
                available_ingredients=available_inv,
                excluded_ingredients=excluded_ingredients,
                dietary_constraints=dietary_constraints,
                limit=15,
            )
            slot_candidates[slot] = cands

        total_candidates = sum(len(c) for c in slot_candidates.values())
        logger.info(
            "[RecipeGroundedService][recipe_candidates_count] total=%d breakfast=%d lunch=%d dinner=%d",
            total_candidates,
            len(slot_candidates[MealType.BREAKFAST]),
            len(slot_candidates[MealType.LUNCH]),
            len(slot_candidates[MealType.DINNER]),
        )

        # Fail closed if any slot has < 7 candidates or total unique candidates < 21
        unique_candidate_ids = {c.recipe_id for cands in slot_candidates.values() for c in cands}
        if any(len(cands) < 7 for cands in slot_candidates.values()) or len(unique_candidate_ids) < 21:
            return RecipeGroundedGenerationResult(
                status=RecipeGroundedStatus.INSUFFICIENT_GROUNDED_RECIPES,
                error_message=(
                    "INSUFFICIENT_GROUNDED_RECIPES: Недостаточно проверенных рецептов в каталоге для составления полного меню. "
                    f"Требуется минимум 7 уникальных рецептов на каждый прием пищи (всего 21), доступно: "
                    f"завтрак={len(slot_candidates[MealType.BREAKFAST])}, "
                    f"обед={len(slot_candidates[MealType.LUNCH])}, "
                    f"ужин={len(slot_candidates[MealType.DINNER])}."
                ),
            )

        # 6. LLM Planning
        dates = week_dates(week_start)
        logger.info("[RecipeGroundedService][recipe_plan_started] week=%s", week_start)
        try:
            plan = self._planner.plan(
                week_start=week_start,
                dates=dates,
                slot_candidates=slot_candidates,
                target_servings=target_servings,
            )
        except PlannerGroundingError as exc:
            logger.warning("[RecipeGroundedService][recipe_plan_validation_failed] error=%s", exc)
            return RecipeGroundedGenerationResult(
                status=RecipeGroundedStatus.PLANNER_FAILED,
                error_message=str(exc),
            )

        # 7. Grounding Validation
        validator = RecipeGroundingValidator(catalog)
        grounding_result = validator.validate_plan(
            plan,
            expected_dates=dates,
            selected_authors=authors,
        )

        logger.info(
            "[RecipeGroundedService][recipe_grounding_coverage] ratio=%.2f coverage=%s valid=%s",
            grounding_result.grounding_ratio,
            grounding_result.source_coverage,
            grounding_result.valid,
        )

        if not grounding_result.valid or not grounding_result.resolved_meals:
            return RecipeGroundedGenerationResult(
                status=RecipeGroundedStatus.GROUNDING_FAILED,
                grounding_result=grounding_result,
                error_message=grounding_result.error_message or "GROUNDING_FAILED",
            )

        # 8. Shopping derivation
        shopping_items = derive_shopping_list_from_recipes(
            grounding_result.resolved_meals,
            confirmed_inventory=confirmed_inv_tuples,
        )
        # 9. Storage translation
        entry_inputs: list[WeeklyMenuEntryInput] = []
        entry_refs_to_save: list[tuple[str, ValidatedMealEntry]] = []

        for meal in grounding_result.resolved_meals:
            ing_inputs = tuple(
                WeeklyMenuIngredientInput(
                    display_name=ing.display_name,
                    quantity_value=_normalize_weekly_menu_ingredient_qty(ing.quantity),
                    quantity_unit=_normalize_weekly_menu_ingredient_unit(ing.unit),
                    recipe_base_servings=str(meal.recipe.servings),
                    position=ing.position,
                )
                for ing in meal.recipe.ingredients
            )
            if not ing_inputs:
                ing_inputs = (
                    WeeklyMenuIngredientInput(
                        display_name=meal.recipe.title,
                        quantity_value="1",
                        quantity_unit="piece",
                        recipe_base_servings=str(meal.recipe.servings),
                        position=1,
                    ),
                )

            desc = f"Автор: {meal.author_display_name}"
            if meal.source_title:
                desc += f" | {meal.source_title}"
            if meal.source_locator:
                desc += f" ({meal.source_locator})"

            entry_input = WeeklyMenuEntryInput(
                local_date=meal.date,
                meal_slot=meal.slot.value,
                position=1,
                title=meal.recipe.title,
                description=desc,
                servings=str(meal.target_servings),
                origin=WeeklyMenuEntryOrigin.GENERATED,
                ingredients=ing_inputs,
            )
            entry_inputs.append(entry_input)
            entry_refs_to_save.append((meal.date, meal))

        # Save draft revision via canonical storage flow
        try:
            # Deterministic plan identity for payload_hash
            catalog_hash = getattr(catalog, "get_content_hash", lambda: "unknown")()
            plan_identity = [
                f"{m.date}:{m.slot.value}:{m.recipe.recipe_id}:{m.recipe.recipe_version}:{m.target_servings}"
                for m in grounding_result.resolved_meals
            ]
            raw_payload = json.dumps(
                {
                    "household_id": context.household_id,
                    "week_start": week_start,
                    "authors": sorted(authors),
                    "target_servings": str(target_servings),
                    "catalog_hash": catalog_hash,
                    "meals": plan_identity,
                },
                sort_keys=True,
            )
            payload_hash = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()

            # Deterministic idempotency key if not provided
            key = idempotency_key or f"rcp_gen:{context.household_id}:{week_start}:{payload_hash[:16]}"

            # 1. Lookup replay
            replay = self._weekly_menu_store.lookup_generated_draft_replay(
                context,
                idempotency_key=key,
                payload_hash=payload_hash,
            )
            if replay is not None:
                return RecipeGroundedGenerationResult(
                    status=RecipeGroundedStatus.SUCCESS,
                    revision_view=replay,
                    grounding_result=grounding_result,
                    shopping_items=tuple(shopping_items),
                )

            # 2. Resolve expected versions
            series = self._weekly_menu_store.get_weekly_menu_series(context, context.household_id, week_start)
            if series is None:
                if expected_series_version is not None:
                    raise WeeklyMenuConflictError("weekly menu series version mismatch")
                exp_series_ver = None
                exp_draft_rev_id = None
                exp_draft_rev_ver = None
            else:
                if expected_series_version is not None and series.version != int(expected_series_version):
                    raise WeeklyMenuConflictError("weekly menu series version mismatch")
                revisions = self._weekly_menu_store.list_weekly_menu_revisions(context, series.id)
                current_draft = next((r for r in revisions if r.status is WeeklyMenuRevisionStatus.DRAFT), None)
                exp_series_ver = series.version
                exp_draft_rev_id = current_draft.id if current_draft else None
                exp_draft_rev_ver = current_draft.version if current_draft else None

            # 3. Apply generated draft entries
            rev_view = self._weekly_menu_store.apply_generated_draft_entries(
                context,
                week_start=week_start,
                entries=entry_inputs,
                expected_series_version=exp_series_ver,
                expected_draft_revision_id=exp_draft_rev_id,
                expected_draft_revision_version=exp_draft_rev_ver,
                idempotency_key=key,
                payload_hash=payload_hash,
            )

            # 4. Link recipe refs to created entries (atomic with success)
            recipe_refs: list[WeeklyMenuEntryRecipeRef] = []
            for entry in rev_view.entries:
                slot_val = entry.meal_slot.value if hasattr(entry.meal_slot, "value") else str(entry.meal_slot)
                matching_meal = next(
                    (m for m in grounding_result.resolved_meals if m.date == entry.local_date and m.slot.value == slot_val),
                    None,
                )
                if matching_meal:
                    ref_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{entry.id}:{matching_meal.recipe.recipe_id}"))
                    recipe_refs.append(
                        WeeklyMenuEntryRecipeRef(
                            id=ref_id,
                            entry_id=entry.id,
                            household_id=context.household_id,
                            recipe_id=matching_meal.recipe.recipe_id,
                            recipe_version=matching_meal.recipe.recipe_version,
                            source_id=matching_meal.recipe.source_id,
                            author_id=matching_meal.recipe.author_id,
                            target_servings=str(matching_meal.target_servings),
                            created_at=entry.created_at,
                        )
                    )

            self._weekly_menu_store.save_entry_recipe_refs(recipe_refs)

            return RecipeGroundedGenerationResult(
                status=RecipeGroundedStatus.SUCCESS,
                revision_view=rev_view,
                grounding_result=grounding_result,
                shopping_items=tuple(shopping_items),
            )
        except WeeklyMenuConflictError as exc:
            logger.warning("[RecipeGroundedService] Weekly menu conflict: %s", exc)
            return RecipeGroundedGenerationResult(
                status=RecipeGroundedStatus.STORAGE_FAILURE,
                error_message=f"Conflict: {exc}",
            )
        except Exception as exc:
            logger.exception("[RecipeGroundedService] Storage failure: %s", exc)
            return RecipeGroundedGenerationResult(
                status=RecipeGroundedStatus.STORAGE_FAILURE,
                error_message=f"Storage failure: {exc}",
            )
