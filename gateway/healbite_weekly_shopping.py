from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from gateway.healbite_feature_gates import (
    FeatureAvailabilityStatus,
    FeatureGateConfig,
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
from gateway.healbite_inventory import (
    INVENTORY_ITEMS_TABLE,
    INVENTORY_SNAPSHOTS_TABLE,
)
from gateway.healbite_nutrition_diary import resolve_healbite_db_path
from gateway.healbite_shopping import (
    HealBiteShoppingStore,
    ShoppingAccessError,
    ShoppingConflictError,
    ShoppingListView,
    ShoppingSchemaError,
    ShoppingStateError,
    ShoppingValidationError,
    _NORMALIZATION_VERSION,
    _derive_weekly_ingredient_rows,
    _derived_base_unit,
    _normalize_identity,
    _normalize_idempotency_key,
    _payload_fingerprint,
    _rounded_quantity,
    _sqlite_timestamp,
)
from gateway.healbite_shopping_schema import (
    SHOPPING_CONTRIBUTIONS_TABLE,
    SHOPPING_IDEMPOTENCY_TABLE,
    SHOPPING_ITEMS_TABLE,
    SHOPPING_LISTS_TABLE,
    ShoppingIdempotencyOperation,
    ShoppingItemOrigin,
    ShoppingItemOverrideState,
    ShoppingListStatus,
    ShoppingSchemaState,
    ShoppingUnit,
    detect_shopping_schema_state,
    new_shopping_contribution_id,
    new_shopping_item_id,
    new_shopping_list_id,
    require_shopping_list_id,
)
from gateway.healbite_weekly_menu_schema import (
    WEEKLY_MENU_ENTRIES_TABLE,
    WEEKLY_MENU_IDEMPOTENCY_TABLE,
    WEEKLY_MENU_INGREDIENTS_TABLE,
    WEEKLY_MENU_REVISIONS_TABLE,
    WEEKLY_MENU_SERIES_TABLE,
    WeeklyMenuIdempotencyOperation,
    WeeklyMenuRevisionStatus,
    WeeklyMenuSchemaState,
    detect_weekly_menu_schema_state,
    new_weekly_menu_idempotency_id,
    require_monday_week_start,
    week_dates,
)
from gateway.healbite_weekly_menus import HouseholdAuthorizationContext


WEEKLY_SHOPPING_OBSERVABILITY_MARKER = (
    "[HealBite][weekly_shopping_observability]"
)
_TOKEN_VERSION = "v1"
_TOKEN_HEX_LENGTH = 32

logger = logging.getLogger(__name__)


class WeeklyShoppingError(RuntimeError):
    pass


class WeeklyShoppingUnavailableError(WeeklyShoppingError):
    def __init__(self, status: FeatureAvailabilityStatus) -> None:
        super().__init__("weekly shopping unavailable")
        self.status = status


class WeeklyShoppingStaleError(WeeklyShoppingError):
    pass


class WeeklyShoppingValidationError(WeeklyShoppingError):
    pass


class WeeklyShoppingStorageError(WeeklyShoppingError):
    pass


class WeeklyShoppingItemState(str, Enum):
    MISSING = "missing"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class WeeklyShoppingContribution:
    source_menu_entry_id: str
    source_ingredient_id: str
    quantity_value: str
    unit: ShoppingUnit


@dataclass(frozen=True, slots=True)
class WeeklyShoppingDeltaItem:
    normalized_name: str
    display_name: str
    quantity_value: str | None
    unit: ShoppingUnit
    state: WeeklyShoppingItemState
    source_menu_entry_id: str
    contributions: tuple[WeeklyShoppingContribution, ...]

    @property
    def needs_review(self) -> bool:
        return self.state is WeeklyShoppingItemState.NEEDS_REVIEW


@dataclass(frozen=True, slots=True)
class WeeklyShoppingDelta:
    week_start: str
    weekly_revision_id: str
    weekly_revision_number: int
    weekly_revision_version: int
    weekly_series_version: int
    inventory_snapshot_id: str
    inventory_source_revision: int
    items: tuple[WeeklyShoppingDeltaItem, ...]

    @property
    def approval_token(self) -> str:
        payload = (
            f"{_TOKEN_VERSION}:{self.weekly_revision_id}:"
            f"{self.weekly_revision_version}:{self.weekly_series_version}:"
            f"{self.inventory_snapshot_id}:{self.inventory_source_revision}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[
            :_TOKEN_HEX_LENGTH
        ]


@dataclass(frozen=True, slots=True)
class WeeklyShoppingApprovalResult:
    delta: WeeklyShoppingDelta | None
    shopping: ShoppingListView
    already_applied: bool = False


def _log_event(action: str, *, result: str) -> None:
    logger.info(
        "%s action=%s result=%s",
        WEEKLY_SHOPPING_OBSERVABILITY_MARKER,
        action,
        result,
    )


def _positive_actor(value: object) -> int:
    if isinstance(value, bool):
        raise WeeklyShoppingUnavailableError(
            FeatureAvailabilityStatus.INVALID_ACTOR
        )
    try:
        actor = int(value)
    except (TypeError, ValueError) as exc:
        raise WeeklyShoppingUnavailableError(
            FeatureAvailabilityStatus.INVALID_ACTOR
        ) from exc
    if actor <= 0:
        raise WeeklyShoppingUnavailableError(
            FeatureAvailabilityStatus.INVALID_ACTOR
        )
    return actor


def _read_only_uri(path: Path) -> str:
    return f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"


def _connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_read_only_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA query_only = ON")
    return conn


def _require_supported_schemas(conn: sqlite3.Connection) -> None:
    if (
        detect_weekly_menu_schema_state(conn)
        is not WeeklyMenuSchemaState.CANONICAL
    ):
        raise WeeklyShoppingStorageError("weekly schema unavailable")
    if (
        detect_shopping_schema_state(conn)
        is not ShoppingSchemaState.CANONICAL
    ):
        raise WeeklyShoppingStorageError("shopping schema unavailable")


def _latest_confirmed_inventory(
    conn: sqlite3.Connection,
    *,
    household_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        f"""
        SELECT *
        FROM {INVENTORY_SNAPSHOTS_TABLE}
        WHERE household_id = ? AND status = 'confirmed'
        ORDER BY confirmed_at DESC, id DESC
        LIMIT 1
        """,
        (household_id,),
    ).fetchone()
    if row is None:
        raise WeeklyShoppingValidationError(
            "confirmed inventory snapshot not found"
        )
    return row


def _revision_row(
    conn: sqlite3.Connection,
    *,
    household_id: str,
    revision_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        f"""
        SELECT r.*, s.week_start, s.version AS series_version
        FROM {WEEKLY_MENU_REVISIONS_TABLE} r
        JOIN {WEEKLY_MENU_SERIES_TABLE} s ON s.id = r.series_id
        WHERE r.id = ? AND r.household_id = ? AND s.household_id = ?
        LIMIT 1
        """,
        (revision_id, household_id, household_id),
    ).fetchone()
    if row is None:
        raise WeeklyShoppingStaleError("weekly revision not found")
    return row


def _current_draft_row(
    conn: sqlite3.Connection,
    *,
    household_id: str,
    week_start: str,
) -> sqlite3.Row:
    row = conn.execute(
        f"""
        SELECT r.*, s.week_start, s.version AS series_version
        FROM {WEEKLY_MENU_REVISIONS_TABLE} r
        JOIN {WEEKLY_MENU_SERIES_TABLE} s ON s.id = r.series_id
        WHERE r.household_id = ?
          AND s.household_id = ?
          AND s.week_start = ?
          AND r.status = ?
        LIMIT 1
        """,
        (
            household_id,
            household_id,
            week_start,
            WeeklyMenuRevisionStatus.DRAFT.value,
        ),
    ).fetchone()
    if row is None:
        raise WeeklyShoppingStaleError("current weekly draft not found")
    return row


def _weekly_ingredient_rows(
    conn: sqlite3.Connection,
    *,
    household_id: str,
    revision_id: str,
    week_start: str,
) -> list[sqlite3.Row]:
    entry_rows = conn.execute(
        f"""
        SELECT id, local_date, meal_slot
        FROM {WEEKLY_MENU_ENTRIES_TABLE}
        WHERE menu_id = ? AND household_id = ?
        ORDER BY local_date, meal_slot, position, id
        """,
        (revision_id, household_id),
    ).fetchall()
    if not entry_rows:
        raise WeeklyShoppingValidationError("weekly draft has no entries")
    expected_slots = {
        (local_date, meal_slot)
        for local_date in week_dates(week_start)
        for meal_slot in ("breakfast", "lunch", "dinner")
    }
    actual_slots = {
        (str(row["local_date"]), str(row["meal_slot"]))
        for row in entry_rows
    }
    if len(entry_rows) != len(expected_slots) or actual_slots != expected_slots:
        raise WeeklyShoppingValidationError(
            "weekly draft does not contain every required meal slot"
        )
    rows = conn.execute(
        f"""
        SELECT e.id AS source_menu_entry_id,
               e.local_date,
               e.meal_slot,
               e.position AS meal_position,
               e.servings AS planned_portions,
               i.id AS source_ingredient_id,
               i.position AS ingredient_position,
               i.display_name,
               i.quantity_value,
               i.quantity_unit,
               i.recipe_base_servings
        FROM {WEEKLY_MENU_ENTRIES_TABLE} e
        JOIN {WEEKLY_MENU_INGREDIENTS_TABLE} i
          ON i.menu_entry_id = e.id
        WHERE e.menu_id = ? AND e.household_id = ?
        ORDER BY e.local_date, e.meal_slot, e.position, e.id, i.position, i.id
        """,
        (revision_id, household_id),
    ).fetchall()
    ingredient_entry_ids = {
        str(row["source_menu_entry_id"]) for row in rows
    }
    if any(str(row["id"]) not in ingredient_entry_ids for row in entry_rows):
        raise WeeklyShoppingValidationError(
            "weekly draft has incomplete ingredient snapshots"
        )
    return list(rows)


def _inventory_availability(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
) -> tuple[
    dict[tuple[str, ShoppingUnit], Decimal],
    dict[str, set[ShoppingUnit]],
    set[str],
]:
    rows = conn.execute(
        f"""
        SELECT normalized_name, display_name, quantity_value, quantity_unit
        FROM {INVENTORY_ITEMS_TABLE}
        WHERE snapshot_id = ?
        ORDER BY position, id
        """,
        (snapshot_id,),
    ).fetchall()
    available: dict[tuple[str, ShoppingUnit], Decimal] = {}
    units_by_name: dict[str, set[ShoppingUnit]] = {}
    unknown_names: set[str] = set()
    for row in rows:
        normalized_name = _normalize_identity(str(row["display_name"]))
        quantity_value = row["quantity_value"]
        quantity_unit = str(row["quantity_unit"])
        if quantity_value is None or quantity_unit == ShoppingUnit.UNKNOWN.value:
            unknown_names.add(normalized_name)
            continue
        try:
            base_unit, factor = _derived_base_unit(quantity_unit)
            quantity = Decimal(str(quantity_value)) * factor
        except Exception as exc:
            raise WeeklyShoppingValidationError(
                "invalid confirmed inventory quantity"
            ) from exc
        units_by_name.setdefault(normalized_name, set()).add(base_unit)
        key = (normalized_name, base_unit)
        available[key] = available.get(key, Decimal("0")) + quantity
    return available, units_by_name, unknown_names


def _needs_review_item(derived: object) -> WeeklyShoppingDeltaItem:
    return WeeklyShoppingDeltaItem(
        normalized_name=str(derived.normalized_name),
        display_name=str(derived.display_name),
        quantity_value=None,
        unit=ShoppingUnit.UNKNOWN,
        state=WeeklyShoppingItemState.NEEDS_REVIEW,
        source_menu_entry_id=str(derived.source_menu_entry_id),
        contributions=tuple(
            WeeklyShoppingContribution(
                source_menu_entry_id=str(value.source_menu_entry_id),
                source_ingredient_id=str(value.source_ingredient_id),
                quantity_value=str(value.scaled_quantity_value),
                unit=derived.quantity_unit,
            )
            for value in derived.contributions
        ),
    )


def _missing_contributions(
    derived: object,
    missing: Decimal,
) -> tuple[WeeklyShoppingContribution, ...]:
    remaining = missing
    contributions: list[WeeklyShoppingContribution] = []
    for value in derived.contributions:
        if remaining <= 0:
            break
        source_quantity = Decimal(str(value.scaled_quantity_value))
        selected = min(source_quantity, remaining)
        if selected <= 0:
            continue
        contributions.append(
            WeeklyShoppingContribution(
                source_menu_entry_id=str(value.source_menu_entry_id),
                source_ingredient_id=str(value.source_ingredient_id),
                quantity_value=_rounded_quantity(selected),
                unit=derived.quantity_unit,
            )
        )
        remaining -= selected
    if remaining > 0:
        raise WeeklyShoppingValidationError(
            "weekly contribution allocation failed"
        )
    return tuple(contributions)


def _calculate_delta_items(
    conn: sqlite3.Connection,
    *,
    household_id: str,
    revision_id: str,
    week_start: str,
    inventory_snapshot_id: str,
) -> tuple[WeeklyShoppingDeltaItem, ...]:
    derived_items = _derive_weekly_ingredient_rows(
        _weekly_ingredient_rows(
            conn,
            household_id=household_id,
            revision_id=revision_id,
            week_start=week_start,
        )
    )
    available, inventory_units, unknown_names = _inventory_availability(
        conn,
        snapshot_id=inventory_snapshot_id,
    )
    delta_items: list[WeeklyShoppingDeltaItem] = []
    for derived in derived_items:
        normalized_name = str(derived.normalized_name)
        required_unit = derived.quantity_unit
        known_units = inventory_units.get(normalized_name, set())
        if normalized_name in unknown_names or (
            known_units and required_unit not in known_units
        ):
            delta_items.append(_needs_review_item(derived))
            continue
        required = Decimal(str(derived.quantity_value))
        missing = required - available.get(
            (normalized_name, required_unit),
            Decimal("0"),
        )
        if missing <= 0:
            continue
        delta_items.append(
            WeeklyShoppingDeltaItem(
                normalized_name=normalized_name,
                display_name=str(derived.display_name),
                quantity_value=_rounded_quantity(missing),
                unit=required_unit,
                state=WeeklyShoppingItemState.MISSING,
                source_menu_entry_id=str(derived.source_menu_entry_id),
                contributions=_missing_contributions(derived, missing),
            )
        )
    return tuple(delta_items)


def _build_delta(
    conn: sqlite3.Connection,
    *,
    household_id: str,
    revision: sqlite3.Row,
    inventory: sqlite3.Row,
) -> WeeklyShoppingDelta:
    if str(revision["status"]) != WeeklyMenuRevisionStatus.DRAFT.value:
        raise WeeklyShoppingStaleError("weekly revision is not a draft")
    if str(inventory["status"]) != "confirmed":
        raise WeeklyShoppingStaleError("inventory snapshot is not confirmed")
    delta = WeeklyShoppingDelta(
        week_start=str(revision["week_start"]),
        weekly_revision_id=str(revision["id"]),
        weekly_revision_number=int(revision["revision_number"]),
        weekly_revision_version=int(revision["version"]),
        weekly_series_version=int(revision["series_version"]),
        inventory_snapshot_id=str(inventory["id"]),
        inventory_source_revision=int(inventory["source_revision"]),
        items=_calculate_delta_items(
            conn,
            household_id=household_id,
            revision_id=str(revision["id"]),
            week_start=str(revision["week_start"]),
            inventory_snapshot_id=str(inventory["id"]),
        ),
    )
    _log_event("weekly_shopping_delta_built", result="success")
    if any(item.needs_review for item in delta.items):
        _log_event("weekly_shopping_needs_review", result="blocked")
    return delta


class HealBiteWeeklyShoppingService:
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        config: FeatureGateConfig | None = None,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self._db_path = resolve_healbite_db_path(db_path)
        self._config = (
            config
            if config is not None
            else load_feature_gate_config("HEALBITE_SHOPPING_LIST")
        )
        self._fault_hook = fault_hook

    def _fault(self, phase: str) -> None:
        if self._fault_hook is not None:
            self._fault_hook(phase)

    def _resolve_context(
        self,
        actor_user_id: object,
    ) -> HouseholdAuthorizationContext:
        actor = _positive_actor(actor_user_id)
        try:
            context = HealBiteHouseholdService(
                HealBiteHouseholdStore(
                    db_path=self._db_path,
                    ensure_schema_on_init=False,
                )
            ).resolve_existing_actor_household_context(actor)
        except (
            HouseholdAccessError,
            HouseholdIntegrityError,
            HouseholdNotFoundError,
            HouseholdValidationError,
            sqlite3.Error,
        ) as exc:
            raise WeeklyShoppingUnavailableError(
                FeatureAvailabilityStatus.HOUSEHOLD_UNAVAILABLE
            ) from exc
        return HouseholdAuthorizationContext.from_household_context(context)

    def preview(
        self,
        actor_user_id: object,
        *,
        revision_id: str,
        inventory_snapshot_id: str,
    ) -> WeeklyShoppingDelta:
        context = self._resolve_context(actor_user_id)
        try:
            with _connect_read_only(self._db_path) as conn:
                _require_supported_schemas(conn)
                revision = _revision_row(
                    conn,
                    household_id=context.household_id,
                    revision_id=revision_id,
                )
                inventory = _latest_confirmed_inventory(
                    conn,
                    household_id=context.household_id,
                )
                if str(inventory["id"]) != str(inventory_snapshot_id):
                    raise WeeklyShoppingStaleError(
                        "inventory snapshot is stale"
                    )
                return _build_delta(
                    conn,
                    household_id=context.household_id,
                    revision=revision,
                    inventory=inventory,
                )
        except WeeklyShoppingError:
            raise
        except (sqlite3.Error, OSError) as exc:
            raise WeeklyShoppingStorageError(
                "weekly shopping preview failed"
            ) from exc

    def approve(
        self,
        actor_user_id: object,
        *,
        week_start: str,
        approval_token: str,
        idempotency_key: str,
    ) -> WeeklyShoppingApprovalResult:
        decision = evaluate_feature_gate(self._config, actor_user_id)
        if not decision.ready:
            raise WeeklyShoppingUnavailableError(decision.status)
        context = self._resolve_context(actor_user_id)
        canonical_week_start = require_monday_week_start(week_start)
        token = str(approval_token).strip().lower()
        if (
            len(token) != _TOKEN_HEX_LENGTH
            or any(character not in "0123456789abcdef" for character in token)
        ):
            raise WeeklyShoppingStaleError("invalid approval token")
        normalized_idempotency_key = _normalize_idempotency_key(
            idempotency_key
        )
        shopping_store = HealBiteShoppingStore(db_path=self._db_path)
        try:
            with shopping_store._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                auth = shopping_store._revalidate_shopping_actor_for_write(
                    conn,
                    context,
                    operation="regenerate",
                )
                _require_supported_schemas(conn)
                existing = self._existing_approval(
                    conn,
                    shopping_store=shopping_store,
                    auth=auth,
                    idempotency_key=normalized_idempotency_key,
                )
                if existing is not None:
                    conn.commit()
                    return WeeklyShoppingApprovalResult(
                        delta=None,
                        shopping=existing,
                        already_applied=True,
                    )
                revision = _current_draft_row(
                    conn,
                    household_id=auth.household_id,
                    week_start=canonical_week_start,
                )
                inventory = _latest_confirmed_inventory(
                    conn,
                    household_id=auth.household_id,
                )
                delta = _build_delta(
                    conn,
                    household_id=auth.household_id,
                    revision=revision,
                    inventory=inventory,
                )
                if delta.approval_token != token:
                    _log_event(
                        "weekly_shopping_stale_revision",
                        result="blocked",
                    )
                    raise WeeklyShoppingStaleError(
                        "weekly shopping approval is stale"
                    )
                payload_hash = _payload_fingerprint(
                    {
                        "approval_token": token,
                        "week_start": canonical_week_start,
                        "items": [
                            {
                                "name": item.normalized_name,
                                "quantity": item.quantity_value,
                                "unit": item.unit.value,
                            }
                            for item in delta.items
                        ],
                    }
                )
                self._publish_revision(
                    conn,
                    auth=auth,
                    revision=revision,
                    idempotency_key=normalized_idempotency_key,
                    payload_hash=payload_hash,
                )
                self._fault("after_menu_publish")
                shopping = self._reconcile_shopping(
                    conn,
                    shopping_store=shopping_store,
                    auth=auth,
                    delta=delta,
                )
                self._fault("after_shopping_reconcile")
                shopping_store._store_idempotency(
                    conn=conn,
                    auth=auth,
                    operation=(
                        ShoppingIdempotencyOperation.REGENERATE_GENERATED_ITEMS
                    ),
                    idempotency_key=normalized_idempotency_key,
                    payload_hash=payload_hash,
                    shopping_list_id=shopping.shopping_list.id,
                    shopping_item_id=None,
                )
                self._fault("before_commit")
                conn.commit()
                _log_event(
                    "weekly_shopping_approval_success",
                    result="success",
                )
                return WeeklyShoppingApprovalResult(
                    delta=delta,
                    shopping=shopping,
                )
        except WeeklyShoppingError:
            _log_event("weekly_shopping_approval_failure", result="blocked")
            raise
        except (
            ShoppingAccessError,
            ShoppingConflictError,
            ShoppingSchemaError,
            ShoppingStateError,
            ShoppingValidationError,
        ) as exc:
            _log_event("weekly_shopping_approval_failure", result="blocked")
            raise WeeklyShoppingValidationError(
                "weekly shopping approval rejected"
            ) from exc
        except (sqlite3.Error, OSError) as exc:
            _log_event("weekly_shopping_approval_failure", result="failure")
            raise WeeklyShoppingStorageError(
                "weekly shopping approval failed"
            ) from exc

    @staticmethod
    def _existing_approval(
        conn: sqlite3.Connection,
        *,
        shopping_store: HealBiteShoppingStore,
        auth: HouseholdAuthorizationContext,
        idempotency_key: str,
    ) -> ShoppingListView | None:
        row = conn.execute(
            f"""
            SELECT shopping_list_id
            FROM {SHOPPING_IDEMPOTENCY_TABLE}
            WHERE household_id = ?
              AND actor_member_id = ?
              AND operation = ?
              AND idempotency_key = ?
            LIMIT 1
            """,
            (
                auth.household_id,
                auth.household_member_id,
                ShoppingIdempotencyOperation.REGENERATE_GENERATED_ITEMS.value,
                idempotency_key,
            ),
        ).fetchone()
        if row is None:
            return None
        shopping_list = shopping_store._get_list_by_id(
            conn,
            require_shopping_list_id(str(row["shopping_list_id"])),
        )
        if shopping_list is None:
            raise WeeklyShoppingStorageError(
                "weekly shopping idempotency target missing"
            )
        shopping_store._assert_list_in_scope(auth, shopping_list)
        return shopping_store._build_list_view(conn, shopping_list)

    @staticmethod
    def _publish_revision(
        conn: sqlite3.Connection,
        *,
        auth: HouseholdAuthorizationContext,
        revision: sqlite3.Row,
        idempotency_key: str,
        payload_hash: str,
    ) -> None:
        if str(revision["status"]) != WeeklyMenuRevisionStatus.DRAFT.value:
            raise WeeklyShoppingStaleError("weekly revision is not a draft")
        now = _sqlite_timestamp()
        current_published = conn.execute(
            f"""
            SELECT id
            FROM {WEEKLY_MENU_REVISIONS_TABLE}
            WHERE series_id = ? AND status = ?
            LIMIT 1
            """,
            (
                str(revision["series_id"]),
                WeeklyMenuRevisionStatus.PUBLISHED.value,
            ),
        ).fetchone()
        if (
            current_published is not None
            and str(current_published["id"]) != str(revision["id"])
        ):
            conn.execute(
                f"""
                UPDATE {WEEKLY_MENU_REVISIONS_TABLE}
                SET status = ?, archived_at = ?, updated_at = ?,
                    version = version + 1
                WHERE id = ? AND status = ?
                """,
                (
                    WeeklyMenuRevisionStatus.ARCHIVED.value,
                    now,
                    now,
                    str(current_published["id"]),
                    WeeklyMenuRevisionStatus.PUBLISHED.value,
                ),
            )
        cursor = conn.execute(
            f"""
            UPDATE {WEEKLY_MENU_REVISIONS_TABLE}
            SET status = ?, published_at = ?, archived_at = NULL,
                updated_at = ?, version = version + 1
            WHERE id = ? AND household_id = ? AND status = ? AND version = ?
            """,
            (
                WeeklyMenuRevisionStatus.PUBLISHED.value,
                now,
                now,
                str(revision["id"]),
                auth.household_id,
                WeeklyMenuRevisionStatus.DRAFT.value,
                int(revision["version"]),
            ),
        )
        if cursor.rowcount != 1:
            raise WeeklyShoppingStaleError("weekly revision changed")
        series_cursor = conn.execute(
            f"""
            UPDATE {WEEKLY_MENU_SERIES_TABLE}
            SET updated_at = ?, version = version + 1
            WHERE id = ? AND household_id = ? AND version = ?
            """,
            (
                now,
                str(revision["series_id"]),
                auth.household_id,
                int(revision["series_version"]),
            ),
        )
        if series_cursor.rowcount != 1:
            raise WeeklyShoppingStaleError("weekly series changed")
        conn.execute(
            f"""
            INSERT INTO {WEEKLY_MENU_IDEMPOTENCY_TABLE}
                (id, household_id, actor_member_id, operation,
                 idempotency_key, payload_fingerprint, series_id,
                 revision_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_weekly_menu_idempotency_id(),
                auth.household_id,
                auth.household_member_id,
                WeeklyMenuIdempotencyOperation.PUBLISH_REVISION.value,
                idempotency_key,
                payload_hash,
                str(revision["series_id"]),
                str(revision["id"]),
                now,
            ),
        )

    def _reconcile_shopping(
        self,
        conn: sqlite3.Connection,
        *,
        shopping_store: HealBiteShoppingStore,
        auth: HouseholdAuthorizationContext,
        delta: WeeklyShoppingDelta,
    ) -> ShoppingListView:
        row = conn.execute(
            f"""
            SELECT *
            FROM {SHOPPING_LISTS_TABLE}
            WHERE household_id = ? AND week_start = ? AND status = ?
            LIMIT 1
            """,
            (
                auth.household_id,
                delta.week_start,
                ShoppingListStatus.ACTIVE.value,
            ),
        ).fetchone()
        now = _sqlite_timestamp()
        if row is None:
            shopping_list_id = new_shopping_list_id()
            conn.execute(
                f"""
                INSERT INTO {SHOPPING_LISTS_TABLE}
                    (id, household_id, week_start, source_menu_id,
                     source_menu_revision, status, created_by_member_id,
                     created_at, updated_at, completed_at, archived_at, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1)
                """,
                (
                    shopping_list_id,
                    auth.household_id,
                    delta.week_start,
                    delta.weekly_revision_id,
                    delta.weekly_revision_number,
                    ShoppingListStatus.ACTIVE.value,
                    auth.household_member_id,
                    now,
                    now,
                ),
            )
        else:
            shopping_list_id = str(row["id"])
            shopping_list = shopping_store._row_to_list(row)
            shopping_store._assert_list_in_scope(auth, shopping_list)
            shopping_store._assert_regeneration_allowed(auth, shopping_list)

        existing_items = shopping_store._list_items_for_list(
            conn,
            shopping_list_id,
        )
        for item in existing_items:
            if (
                item.origin is not ShoppingItemOrigin.MENU_GENERATED
                or item.override_state is not ShoppingItemOverrideState.NONE
            ):
                continue
            if item.checked_state:
                conn.execute(
                    f"""
                    UPDATE {SHOPPING_ITEMS_TABLE}
                    SET override_state = ?, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (
                        ShoppingItemOverrideState.MANUALIZED.value,
                        now,
                        item.id,
                    ),
                )
            else:
                conn.execute(
                    f"""
                    UPDATE {SHOPPING_IDEMPOTENCY_TABLE}
                    SET shopping_item_id = NULL
                    WHERE shopping_item_id = ?
                    """,
                    (item.id,),
                )
                conn.execute(
                    f"DELETE FROM {SHOPPING_ITEMS_TABLE} WHERE id = ?",
                    (item.id,),
                )
        self._fault("after_generated_replacement")

        next_position = 1_000_000 + len(existing_items)
        for index, item in enumerate(delta.items):
            item_id = new_shopping_item_id()
            fingerprint = _payload_fingerprint(
                {
                    "normalized_name": item.normalized_name,
                    "quantity_unit": item.unit.value,
                    "state": item.state.value,
                    "source": "weekly_inventory_delta",
                    "normalization_version": _NORMALIZATION_VERSION,
                }
            )
            conn.execute(
                f"""
                INSERT INTO {SHOPPING_ITEMS_TABLE}
                    (id, shopping_list_id, household_id, normalized_name,
                     display_name, quantity_value, quantity_unit_normalized,
                     quantity_unit_display, category, position, checked_state,
                     origin, override_state, source_menu_entry_id,
                     normalization_version, dedup_fingerprint, created_at,
                     updated_at, version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    item_id,
                    shopping_list_id,
                    auth.household_id,
                    item.normalized_name,
                    item.display_name,
                    item.quantity_value,
                    item.unit.value,
                    item.unit.value,
                    next_position,
                    ShoppingItemOrigin.MENU_GENERATED.value,
                    ShoppingItemOverrideState.NONE.value,
                    item.source_menu_entry_id,
                    _NORMALIZATION_VERSION,
                    fingerprint,
                    now,
                    now,
                ),
            )
            next_position += 1
            for contribution in item.contributions:
                conn.execute(
                    f"""
                    INSERT INTO {SHOPPING_CONTRIBUTIONS_TABLE}
                        (id, shopping_item_id, source_menu_entry_id,
                         source_ingredient_id, scaled_quantity_value,
                         quantity_unit_normalized, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_shopping_contribution_id(),
                        item_id,
                        contribution.source_menu_entry_id,
                        contribution.source_ingredient_id,
                        contribution.quantity_value,
                        contribution.unit.value,
                        now,
                    ),
                )
            if index == 0:
                self._fault("after_first_generated_insert")

        shopping_store._normalize_positions(
            conn,
            shopping_list_id=shopping_list_id,
        )
        conn.execute(
            f"""
            UPDATE {SHOPPING_LISTS_TABLE}
            SET source_menu_id = ?, source_menu_revision = ?,
                updated_at = ?, version = version + 1
            WHERE id = ?
            """,
            (
                delta.weekly_revision_id,
                delta.weekly_revision_number,
                now,
                shopping_list_id,
            ),
        )
        updated = shopping_store._get_list_by_id(conn, shopping_list_id)
        if updated is None:
            raise WeeklyShoppingStorageError(
                "shopping reconciliation target missing"
            )
        _log_event("weekly_shopping_delta_reconciled", result="success")
        return shopping_store._build_list_view(conn, updated)


def build_weekly_shopping_service(
    *,
    env: dict[str, str] | None = None,
    db_path: str | Path | None = None,
) -> HealBiteWeeklyShoppingService:
    return HealBiteWeeklyShoppingService(
        db_path=db_path,
        config=load_feature_gate_config(
            "HEALBITE_SHOPPING_LIST",
            env=env,
        ),
    )
