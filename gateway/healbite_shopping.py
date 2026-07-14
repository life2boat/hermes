from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote

from gateway.healbite_household_schema import require_canonical_uuid4
from gateway.healbite_households import (
    HealBiteHouseholdStore,
    HouseholdAccessError,
    HouseholdContext,
    HouseholdIntegrityError,
    HouseholdMemberStatus,
    HouseholdNotFoundError,
    HouseholdRole,
    HouseholdStatus,
    HouseholdValidationError,
)
from gateway.healbite_nutrition_diary import resolve_healbite_db_path
from gateway.healbite_weekly_menu_schema import (
    WEEKLY_MENU_ENTRIES_TABLE,
    WEEKLY_MENU_INGREDIENTS_TABLE,
    WEEKLY_MENU_REVISIONS_TABLE,
    WEEKLY_MENU_SERIES_TABLE,
    WeeklyMenuRevisionStatus,
    detect_weekly_menu_schema_state,
    is_valid_week_start,
    parse_iso_local_date,
    require_monday_week_start,
)
from gateway.healbite_weekly_menus import HouseholdAuthorizationContext
from gateway.healbite_shopping_schema import (
    SHOPPING_CONTRIBUTIONS_TABLE,
    SHOPPING_IDEMPOTENCY_OPERATIONS,
    SHOPPING_IDEMPOTENCY_TABLE,
    SHOPPING_ITEM_ORIGINS,
    SHOPPING_ITEM_OVERRIDE_STATES,
    SHOPPING_ITEMS_TABLE,
    SHOPPING_LIST_STATUSES,
    SHOPPING_LISTS_TABLE,
    SHOPPING_SCHEMA_SQL,
    SHOPPING_UNITS,
    ShoppingIdempotencyOperation,
    ShoppingItemOrigin,
    ShoppingItemOverrideState,
    ShoppingListStatus,
    ShoppingSchemaState,
    ShoppingUnit,
    ShoppingUnitFamily,
    detect_shopping_schema_state,
    is_legacy_shopping_schema_without_contributions,
    is_valid_quantity_value,
    new_shopping_idempotency_id,
    new_shopping_contribution_id,
    new_shopping_item_id,
    new_shopping_list_id,
    normalize_quantity_value,
    normalize_shopping_unit,
    quantity_contract_is_valid,
    require_shopping_item_id,
    require_shopping_list_id,
    shopping_unit_family,
    units_are_compatible,
)

_SQLITE_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_ID_REGENERATION_ATTEMPTS = 5
_MAX_DISPLAY_NAME_LENGTH = 200
_MAX_CATEGORY_LENGTH = 64
_MAX_IDEMPOTENCY_KEY_LENGTH = 128
_NORMALIZATION_VERSION = 1
_DERIVATION_QUANTUM = Decimal("0.001")
_UNSET = object()


class ShoppingError(Exception):
    pass


class ShoppingValidationError(ValueError):
    pass


class ShoppingAccessError(ShoppingError):
    pass


class ShoppingConflictError(ShoppingError):
    pass


class ShoppingNotFoundError(ShoppingError):
    pass


class ShoppingStateError(ShoppingError):
    pass


class ShoppingSchemaError(ShoppingError):
    pass


@dataclass(slots=True, frozen=True)
class ShoppingList:
    id: str
    household_id: str
    week_start: str
    source_menu_id: str | None
    source_menu_revision: int | None
    status: ShoppingListStatus
    created_by_member_id: str
    created_at: str
    updated_at: str
    completed_at: str | None
    archived_at: str | None
    version: int


@dataclass(slots=True, frozen=True)
class ShoppingItem:
    id: str
    shopping_list_id: str
    household_id: str
    normalized_name: str
    display_name: str
    quantity_value: str | None
    quantity_unit_normalized: ShoppingUnit
    quantity_unit_display: str
    category: str | None
    position: int
    checked_state: bool
    origin: ShoppingItemOrigin
    override_state: ShoppingItemOverrideState
    source_menu_entry_id: str | None
    normalization_version: int
    dedup_fingerprint: str
    created_at: str
    updated_at: str
    version: int


@dataclass(slots=True, frozen=True)
class ShoppingListView:
    shopping_list: ShoppingList
    items: tuple[ShoppingItem, ...]


@dataclass(slots=True, frozen=True)
class ShoppingSchemaAudit:
    schema_state: ShoppingSchemaState
    list_count: int
    item_count: int
    idempotency_count: int
    orphan_list_count: int
    orphan_item_count: int
    invalid_uuid_count: int
    invalid_status_count: int
    invalid_version_count: int
    invalid_origin_count: int
    invalid_checked_count: int
    invalid_quantity_count: int
    invalid_unit_count: int
    invalid_normalization_version_count: int
    multiple_active_list_count: int
    cross_household_source_mismatch_count: int
    source_revision_missing_count: int
    source_entry_mismatch_count: int
    manual_item_with_source_reference_count: int
    generated_item_without_source_semantics_count: int
    duplicate_deterministic_key_count: int


@dataclass(slots=True, frozen=True)
class ManualShoppingItemInput:
    display_name: str
    quantity_value: str | None = None
    quantity_unit_normalized: ShoppingUnit | str = ShoppingUnit.UNKNOWN
    quantity_unit_display: str | None = None
    category: str | None = None
    position: int | None = None


@dataclass(slots=True, frozen=True)
class GeneratedShoppingItemInput:
    display_name: str
    quantity_value: str | None = None
    quantity_unit_normalized: ShoppingUnit | str = ShoppingUnit.UNKNOWN
    quantity_unit_display: str | None = None
    category: str | None = None
    source_menu_entry_id: str | None = None


def _sqlite_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.strftime(_SQLITE_TS_FORMAT)


def _sqlite_read_only_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro"


def _payload_fingerprint(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise ShoppingValidationError(f"invalid {label}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ShoppingValidationError(f"invalid {label}") from exc
    if parsed <= 0:
        raise ShoppingValidationError(f"invalid {label}")
    return parsed


def _normalize_version(value: object, *, label: str) -> int:
    version = _normalize_positive_int(value, label=label)
    if version < 1:
        raise ShoppingValidationError(f"invalid {label}")
    return version


def _normalize_idempotency_key(value: str) -> str:
    key = str(value).strip()
    if not key or len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ShoppingValidationError("invalid idempotency key")
    return key


def _scoped_idempotency_key(operation: str, value: str) -> str:
    key = _normalize_idempotency_key(value)
    digest = hashlib.sha256(f"{operation}:{key}".encode("utf-8")).hexdigest()
    return f"{operation}:{digest}"


def _normalize_display_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value))
    text = " ".join(text.split())
    if not text or len(text) > _MAX_DISPLAY_NAME_LENGTH:
        raise ShoppingValidationError("invalid display_name")
    return text


def _normalize_category(value: str | None) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = " ".join(text.split())
    if not text:
        return None
    if len(text) > _MAX_CATEGORY_LENGTH:
        raise ShoppingValidationError("invalid category")
    return text


def _normalize_identity(display_name: str) -> str:
    normalized = unicodedata.normalize("NFKC", display_name).casefold()
    return " ".join(normalized.split())


def _normalize_display_unit(
    normalized_unit: ShoppingUnit,
    value: str | None,
) -> str:
    if value is None:
        return normalized_unit.value
    display = unicodedata.normalize("NFKC", str(value))
    display = " ".join(display.split()).lower()
    if not display or len(display) > 32:
        raise ShoppingValidationError("invalid quantity_unit_display")
    if display != normalized_unit.value:
        raise ShoppingValidationError("invalid quantity_unit_display")
    return display


def _normalize_checked_state(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise ShoppingValidationError("invalid checked_state")


def _normalized_generated_payload(
    item: GeneratedShoppingItemInput,
) -> dict[str, object]:
    display_name = _normalize_display_name(item.display_name)
    normalized_name = _normalize_identity(display_name)
    normalized_unit = normalize_shopping_unit(item.quantity_unit_normalized)
    quantity_value = normalize_quantity_value(item.quantity_value)
    if not quantity_contract_is_valid(quantity_value, normalized_unit):
        raise ShoppingValidationError("invalid quantity/unit contract")
    category = _normalize_category(item.category)
    source_menu_entry_id = None
    if item.source_menu_entry_id is not None:
        source_menu_entry_id = require_canonical_uuid4(str(item.source_menu_entry_id))
    display_unit = _normalize_display_unit(normalized_unit, item.quantity_unit_display)
    return {
        "display_name": display_name,
        "normalized_name": normalized_name,
        "quantity_value": quantity_value,
        "quantity_unit_normalized": normalized_unit,
        "quantity_unit_display": display_unit,
        "category": category,
        "source_menu_entry_id": source_menu_entry_id,
    }


def _normalized_manual_payload(
    item: ManualShoppingItemInput,
) -> dict[str, object]:
    payload = _normalized_generated_payload(
        GeneratedShoppingItemInput(
            display_name=item.display_name,
            quantity_value=item.quantity_value,
            quantity_unit_normalized=item.quantity_unit_normalized,
            quantity_unit_display=item.quantity_unit_display,
            category=item.category,
            source_menu_entry_id=None,
        )
    )
    payload["position"] = None if item.position is None else _normalize_positive_int(item.position, label="position")
    return payload


def _dedup_fingerprint(
    *,
    normalized_name: str,
    quantity_unit_normalized: ShoppingUnit,
    category: str | None,
    origin: ShoppingItemOrigin,
    override_state: ShoppingItemOverrideState,
    source_lineage_token: str | None,
    normalization_version: int,
) -> str:
    payload = {
        "normalized_name": normalized_name,
        "quantity_unit_normalized": quantity_unit_normalized.value,
        "category": category or "",
        "origin": origin.value,
        "override_state": override_state.value,
        "source_lineage_token": source_lineage_token or "",
        "normalization_version": int(normalization_version),
    }
    return _payload_fingerprint(payload)


def _sorted_generated_payloads(
    items: Iterable[GeneratedShoppingItemInput],
) -> list[dict[str, object]]:
    return [_normalized_generated_payload(item) for item in items]


def _serialize_generated_payload(payload: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, Enum):
            result[key] = value.value
        else:
            result[key] = value
    return result


def _aggregate_generated_payloads(
    items: Sequence[GeneratedShoppingItemInput],
) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for payload in _sorted_generated_payloads(items):
        lineage_token = str(payload["source_menu_entry_id"] or "")
        fingerprint = _dedup_fingerprint(
            normalized_name=str(payload["normalized_name"]),
            quantity_unit_normalized=payload["quantity_unit_normalized"],
            category=payload["category"],
            origin=ShoppingItemOrigin.MENU_GENERATED,
            override_state=ShoppingItemOverrideState.NONE,
            source_lineage_token=lineage_token,
            normalization_version=_NORMALIZATION_VERSION,
        )
        payload["dedup_fingerprint"] = fingerprint
        grouped.setdefault(fingerprint, []).append(payload)
    aggregated: list[dict[str, object]] = []
    for fingerprint, group in grouped.items():
        if len(group) == 1:
            aggregated.append(group[0])
            continue
        base = group[0]
        normalized_unit = base["quantity_unit_normalized"]
        if normalized_unit is ShoppingUnit.UNKNOWN:
            raise ShoppingValidationError("ambiguous generated deduplication")
        quantities = [payload["quantity_value"] for payload in group]
        if any(value is None for value in quantities):
            raise ShoppingValidationError("ambiguous generated deduplication")
        total = sum(Decimal(str(value)) for value in quantities if value is not None)
        aggregated_payload = dict(base)
        aggregated_payload["quantity_value"] = normalize_quantity_value(str(total))
        aggregated_payload["dedup_fingerprint"] = fingerprint
        aggregated.append(aggregated_payload)
    return aggregated


@dataclass(slots=True, frozen=True)
class _DerivedContribution:
    source_menu_entry_id: str
    source_ingredient_id: str
    scaled_quantity_value: str


@dataclass(slots=True, frozen=True)
class _DerivedShoppingItem:
    display_name: str
    normalized_name: str
    quantity_value: str
    quantity_unit: ShoppingUnit
    source_menu_entry_id: str
    dedup_fingerprint: str
    contributions: tuple[_DerivedContribution, ...]


def _positive_decimal(value: object, *, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ShoppingValidationError(f"invalid {label}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ShoppingValidationError(f"invalid {label}")
    return parsed


def _derived_base_unit(value: object) -> tuple[ShoppingUnit, Decimal]:
    unit = str(value).strip().lower()
    mapping = {
        "g": (ShoppingUnit.G, Decimal("1")),
        "kg": (ShoppingUnit.G, Decimal("1000")),
        "ml": (ShoppingUnit.ML, Decimal("1")),
        "l": (ShoppingUnit.ML, Decimal("1000")),
        "piece": (ShoppingUnit.PIECE, Decimal("1")),
        "package": (ShoppingUnit.PACKAGE, Decimal("1")),
        "unitless": (ShoppingUnit.UNITLESS, Decimal("1")),
    }
    try:
        return mapping[unit]
    except KeyError as exc:
        raise ShoppingValidationError("invalid ingredient unit") from exc


def _rounded_quantity(value: Decimal) -> str:
    """Round each source contribution to 0.001 before aggregation."""
    if not value.is_finite() or value <= 0:
        raise ShoppingValidationError("scaled ingredient quantity is invalid")
    try:
        rounded = value.quantize(_DERIVATION_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ShoppingValidationError("scaled ingredient quantity is invalid") from exc
    if rounded <= 0:
        raise ShoppingValidationError("scaled ingredient quantity is too small")
    try:
        normalized = normalize_quantity_value(format(rounded, "f"))
    except ValueError as exc:
        raise ShoppingValidationError("scaled ingredient quantity is invalid") from exc
    if normalized is None:
        raise ShoppingValidationError("scaled ingredient quantity is invalid")
    return normalized


def _derive_weekly_ingredient_rows(
    rows: Sequence[sqlite3.Row],
) -> list[_DerivedShoppingItem]:
    grouped: dict[
        tuple[str, ShoppingUnit],
        list[tuple[tuple[object, ...], str, _DerivedContribution]],
    ] = {}
    for row in rows:
        display_name = _normalize_display_name(str(row["display_name"]))
        normalized_name = _normalize_identity(display_name)
        base_quantity = _positive_decimal(
            row["quantity_value"],
            label="ingredient quantity",
        )
        base_servings = _positive_decimal(
            row["recipe_base_servings"],
            label="recipe base servings",
        )
        planned_portions = _positive_decimal(
            row["planned_portions"],
            label="planned portions",
        )
        canonical_unit, factor = _derived_base_unit(row["quantity_unit"])
        scaled = _rounded_quantity(
            base_quantity * planned_portions * factor / base_servings
        )
        source_entry_id = require_canonical_uuid4(str(row["source_menu_entry_id"]))
        source_ingredient_id = require_canonical_uuid4(
            str(row["source_ingredient_id"])
        )
        source_order = (
            str(row["local_date"]),
            str(row["meal_slot"]),
            int(row["meal_position"]),
            source_entry_id,
            int(row["ingredient_position"]),
            source_ingredient_id,
        )
        contribution = _DerivedContribution(
            source_menu_entry_id=source_entry_id,
            source_ingredient_id=source_ingredient_id,
            scaled_quantity_value=scaled,
        )
        grouped.setdefault((normalized_name, canonical_unit), []).append(
            (source_order, display_name, contribution)
        )

    derived: list[_DerivedShoppingItem] = []
    for (normalized_name, canonical_unit), values in grouped.items():
        ordered = sorted(values, key=lambda value: value[0])
        contributions = tuple(
            sorted(
                (value[2] for value in ordered),
                key=lambda value: value.source_ingredient_id,
            )
        )
        quantity_value = _rounded_quantity(
            sum(
                (Decimal(value.scaled_quantity_value) for value in contributions),
                Decimal("0"),
            )
        )
        fingerprint = _payload_fingerprint(
            {
                "normalized_name": normalized_name,
                "quantity_value": quantity_value,
                "quantity_unit": canonical_unit.value,
                "normalization_version": _NORMALIZATION_VERSION,
            }
        )
        derived.append(
            _DerivedShoppingItem(
                display_name=ordered[0][1],
                normalized_name=normalized_name,
                quantity_value=quantity_value,
                quantity_unit=canonical_unit,
                source_menu_entry_id=min(
                    value.source_menu_entry_id for value in contributions
                ),
                dedup_fingerprint=fingerprint,
                contributions=contributions,
            )
        )
    return sorted(
        derived,
        key=lambda item: (
            item.normalized_name,
            shopping_unit_family(item.quantity_unit).value,
            item.quantity_unit.value,
            item.dedup_fingerprint,
        ),
    )


class HealBiteShoppingStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        shopping_list_id_factory: Callable[[], str] = new_shopping_list_id,
        shopping_item_id_factory: Callable[[], str] = new_shopping_item_id,
        contribution_id_factory: Callable[[], str] = new_shopping_contribution_id,
        idempotency_id_factory: Callable[[], str] = new_shopping_idempotency_id,
        derivation_fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.db_path = resolve_healbite_db_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._shopping_list_id_factory = shopping_list_id_factory
        self._shopping_item_id_factory = shopping_item_id_factory
        self._contribution_id_factory = contribution_id_factory
        self._idempotency_id_factory = idempotency_id_factory
        self._derivation_fault_hook = derivation_fault_hook
        self._household_store = HealBiteHouseholdStore(
            db_path=self.db_path,
            ensure_schema_on_init=False,
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _owned_connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        except BaseException:
            try:
                conn.close()
            except Exception:
                pass
            raise
        else:
            conn.close()

    @staticmethod
    def _rollback_preserving_error(conn: sqlite3.Connection) -> None:
        try:
            conn.rollback()
        except Exception:
            pass

    def _read_only_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(_sqlite_read_only_uri(self.db_path), uri=True, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _schema_statements() -> tuple[str, ...]:
        return tuple(statement.strip() for statement in SHOPPING_SCHEMA_SQL.split(";") if statement.strip())

    @classmethod
    def schema_statements(cls) -> tuple[str, ...]:
        """Return authoritative shopping DDL without applying it."""
        return cls._schema_statements()

    @classmethod
    def apply_schema(cls, conn: sqlite3.Connection) -> None:
        """Apply authoritative shopping DDL to a borrowed connection."""
        for statement in cls.schema_statements():
            conn.execute(statement)

    def schema_state(self) -> ShoppingSchemaState:
        if not self.db_path.exists():
            return ShoppingSchemaState.DEPENDENCY_MISSING
        with self._owned_connection() as conn:
            return detect_shopping_schema_state(conn)

    def initialize_schema(self) -> ShoppingSchemaState:
        with self._owned_connection() as conn:
            state = detect_shopping_schema_state(conn)
            if state is ShoppingSchemaState.CANONICAL:
                return state
            if state is ShoppingSchemaState.INCOMPATIBLE:
                raise ShoppingSchemaError("shopping schema is incompatible")
            if state is ShoppingSchemaState.DEPENDENCY_MISSING:
                raise ShoppingSchemaError("shopping schema dependency missing")
            try:
                conn.execute("BEGIN IMMEDIATE")
                self.apply_schema(conn)
                final = detect_shopping_schema_state(conn)
                if final is not ShoppingSchemaState.CANONICAL:
                    raise ShoppingSchemaError("shopping schema initialization failed")
                conn.commit()
            except BaseException:
                self._rollback_preserving_error(conn)
                raise
            return final

    def audit_schema(self) -> ShoppingSchemaAudit:
        state = self.schema_state()
        if state in {ShoppingSchemaState.NOT_INITIALIZED, ShoppingSchemaState.DEPENDENCY_MISSING}:
            return ShoppingSchemaAudit(
                schema_state=state,
                list_count=0,
                item_count=0,
                idempotency_count=0,
                orphan_list_count=0,
                orphan_item_count=0,
                invalid_uuid_count=0,
                invalid_status_count=0,
                invalid_version_count=0,
                invalid_origin_count=0,
                invalid_checked_count=0,
                invalid_quantity_count=0,
                invalid_unit_count=0,
                invalid_normalization_version_count=0,
                multiple_active_list_count=0,
                cross_household_source_mismatch_count=0,
                source_revision_missing_count=0,
                source_entry_mismatch_count=0,
                manual_item_with_source_reference_count=0,
                generated_item_without_source_semantics_count=0,
                duplicate_deterministic_key_count=0,
            )
        with self._read_only_connect() as conn:
            if detect_shopping_schema_state(conn) is not ShoppingSchemaState.CANONICAL:
                raise ShoppingSchemaError("shopping schema is not canonical")
            list_rows = conn.execute(f"SELECT * FROM {SHOPPING_LISTS_TABLE}").fetchall()
            item_rows = conn.execute(f"SELECT * FROM {SHOPPING_ITEMS_TABLE}").fetchall()
            idem_rows = conn.execute(f"SELECT * FROM {SHOPPING_IDEMPOTENCY_TABLE}").fetchall()
            invalid_uuid_count = 0
            invalid_status_count = 0
            invalid_version_count = 0
            invalid_origin_count = 0
            invalid_checked_count = 0
            invalid_quantity_count = 0
            invalid_unit_count = 0
            invalid_normalization_version_count = 0
            for row in list_rows:
                if not _is_valid_uuid(row["id"]) or not _is_valid_uuid(row["household_id"]) or not _is_valid_uuid(row["created_by_member_id"]):
                    invalid_uuid_count += 1
                if row["source_menu_id"] is not None and not _is_valid_uuid(row["source_menu_id"]):
                    invalid_uuid_count += 1
                if str(row["status"]) not in SHOPPING_LIST_STATUSES or not is_valid_week_start(str(row["week_start"])):
                    invalid_status_count += 1
                if int(row["version"]) < 1:
                    invalid_version_count += 1
            for row in item_rows:
                if any(not _is_valid_uuid(row[column]) for column in ("id", "shopping_list_id", "household_id")):
                    invalid_uuid_count += 1
                if row["source_menu_entry_id"] is not None and not _is_valid_uuid(row["source_menu_entry_id"]):
                    invalid_uuid_count += 1
                if str(row["origin"]) not in SHOPPING_ITEM_ORIGINS or str(row["override_state"]) not in SHOPPING_ITEM_OVERRIDE_STATES:
                    invalid_origin_count += 1
                if int(row["checked_state"]) not in (0, 1):
                    invalid_checked_count += 1
                if not is_valid_quantity_value(row["quantity_value"]):
                    invalid_quantity_count += 1
                if str(row["quantity_unit_normalized"]) not in SHOPPING_UNITS:
                    invalid_unit_count += 1
                if int(row["normalization_version"]) < 1:
                    invalid_normalization_version_count += 1
                if int(row["version"]) < 1 or int(row["position"]) < 1:
                    invalid_version_count += 1
            orphan_list_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {SHOPPING_LISTS_TABLE} l
                    LEFT JOIN households h ON h.id = l.household_id
                    WHERE h.id IS NULL
                    """
                ).fetchone()[0]
            )
            orphan_item_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {SHOPPING_ITEMS_TABLE} i
                    LEFT JOIN {SHOPPING_LISTS_TABLE} l ON l.id = i.shopping_list_id
                    WHERE l.id IS NULL
                    """
                ).fetchone()[0]
            )
            multiple_active_list_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM (
                        SELECT household_id, week_start
                        FROM {SHOPPING_LISTS_TABLE}
                        WHERE status = ?
                        GROUP BY household_id, week_start
                        HAVING COUNT(*) > 1
                    )
                    """,
                    (ShoppingListStatus.ACTIVE.value,),
                ).fetchone()[0]
            )
            cross_household_source_mismatch_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {SHOPPING_LISTS_TABLE} l
                    JOIN {WEEKLY_MENU_REVISIONS_TABLE} r ON r.id = l.source_menu_id
                    WHERE l.source_menu_id IS NOT NULL AND l.household_id != r.household_id
                    """
                ).fetchone()[0]
            )
            source_revision_missing_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {SHOPPING_LISTS_TABLE} l
                    LEFT JOIN {WEEKLY_MENU_REVISIONS_TABLE} r ON r.id = l.source_menu_id
                    WHERE l.source_menu_id IS NOT NULL AND r.id IS NULL
                    """
                ).fetchone()[0]
            )
            source_entry_mismatch_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {SHOPPING_ITEMS_TABLE} i
                    LEFT JOIN {WEEKLY_MENU_ENTRIES_TABLE} e ON e.id = i.source_menu_entry_id
                    LEFT JOIN {SHOPPING_LISTS_TABLE} l ON l.id = i.shopping_list_id
                    WHERE i.source_menu_entry_id IS NOT NULL
                      AND (
                        e.id IS NULL
                        OR e.household_id != i.household_id
                        OR (l.source_menu_id IS NOT NULL AND e.menu_id != l.source_menu_id)
                      )
                    """
                ).fetchone()[0]
            )
            manual_item_with_source_reference_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {SHOPPING_ITEMS_TABLE}
                    WHERE origin = ? AND source_menu_entry_id IS NOT NULL
                    """,
                    (ShoppingItemOrigin.MANUAL.value,),
                ).fetchone()[0]
            )
            generated_item_without_source_semantics_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {SHOPPING_ITEMS_TABLE} i
                    JOIN {SHOPPING_LISTS_TABLE} l ON l.id = i.shopping_list_id
                    WHERE i.origin = ?
                      AND i.override_state = ?
                      AND l.source_menu_id IS NULL
                      AND i.source_menu_entry_id IS NOT NULL
                    """,
                    (ShoppingItemOrigin.MENU_GENERATED.value, ShoppingItemOverrideState.NONE.value),
                ).fetchone()[0]
            )
            duplicate_deterministic_key_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM (
                        SELECT shopping_list_id, dedup_fingerprint
                        FROM {SHOPPING_ITEMS_TABLE}
                        WHERE origin = ? AND override_state = ?
                        GROUP BY shopping_list_id, dedup_fingerprint
                        HAVING COUNT(*) > 1
                    )
                    """,
                    (ShoppingItemOrigin.MENU_GENERATED.value, ShoppingItemOverrideState.NONE.value),
                ).fetchone()[0]
            )
        return ShoppingSchemaAudit(
            schema_state=state,
            list_count=len(list_rows),
            item_count=len(item_rows),
            idempotency_count=len(idem_rows),
            orphan_list_count=orphan_list_count,
            orphan_item_count=orphan_item_count,
            invalid_uuid_count=invalid_uuid_count,
            invalid_status_count=invalid_status_count,
            invalid_version_count=invalid_version_count,
            invalid_origin_count=invalid_origin_count,
            invalid_checked_count=invalid_checked_count,
            invalid_quantity_count=invalid_quantity_count,
            invalid_unit_count=invalid_unit_count,
            invalid_normalization_version_count=invalid_normalization_version_count,
            multiple_active_list_count=multiple_active_list_count,
            cross_household_source_mismatch_count=cross_household_source_mismatch_count,
            source_revision_missing_count=source_revision_missing_count,
            source_entry_mismatch_count=source_entry_mismatch_count,
            manual_item_with_source_reference_count=manual_item_with_source_reference_count,
            generated_item_without_source_semantics_count=generated_item_without_source_semantics_count,
            duplicate_deterministic_key_count=duplicate_deterministic_key_count,
        )

    def create_shopping_list(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        household_id: str,
        *,
        week_start: str,
        idempotency_key: str,
        source_menu_id: str | None = None,
    ) -> ShoppingListView:
        auth = self._authorize(context, household_id=household_id, operation="list_lifecycle")
        self._require_canonical_schema()
        canonical_week_start = require_monday_week_start(week_start)
        normalized_key = _normalize_idempotency_key(idempotency_key)
        source_payload = None if source_menu_id is None else require_canonical_uuid4(source_menu_id)
        payload_hash = _payload_fingerprint(
            {
                "household_id": auth.household_id,
                "week_start": canonical_week_start,
                "source_menu_id": source_payload,
            }
        )
        for attempt in range(_MAX_ID_REGENERATION_ATTEMPTS):
            with self._connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    auth = self._revalidate_shopping_actor_for_write(
                        conn,
                        context,
                        operation="list_lifecycle",
                    )
                    existing = self._resolve_idempotent_list(
                        conn=conn,
                        auth=auth,
                        operation=ShoppingIdempotencyOperation.CREATE_LIST,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                    )
                    if existing is not None:
                        conn.commit()
                        return self._build_list_view(conn, existing)
                    source_revision_number = None
                    if source_payload is not None:
                        source_revision = self._validate_source_menu_revision(
                            conn,
                            household_id=auth.household_id,
                            source_menu_id=source_payload,
                            week_start=canonical_week_start,
                            allow_archived=True,
                        )
                        source_revision_number = source_revision["revision_number"]
                    now = _sqlite_timestamp()
                    shopping_list_id = require_shopping_list_id(self._shopping_list_id_factory())
                    conn.execute(
                        f"""
                        INSERT INTO {SHOPPING_LISTS_TABLE}
                            (id, household_id, week_start, source_menu_id, source_menu_revision, status,
                             created_by_member_id, created_at, updated_at, completed_at, archived_at, version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1)
                        """,
                        (
                            shopping_list_id,
                            auth.household_id,
                            canonical_week_start,
                            source_payload,
                            source_revision_number,
                            ShoppingListStatus.DRAFT.value,
                            auth.household_member_id,
                            now,
                            now,
                        ),
                    )
                    self._store_idempotency(
                        conn=conn,
                        auth=auth,
                        operation=ShoppingIdempotencyOperation.CREATE_LIST,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                        shopping_list_id=shopping_list_id,
                        shopping_item_id=None,
                    )
                    created = self._get_list_by_id(conn, shopping_list_id)
                    if created is None:
                        raise ShoppingStateError("shopping list creation failed")
                    conn.commit()
                    return self._build_list_view(conn, created)
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    if "UNIQUE" not in str(exc).upper():
                        raise ShoppingConflictError("shopping list integrity conflict") from None
                    if attempt == _MAX_ID_REGENERATION_ATTEMPTS - 1:
                        raise ShoppingConflictError("shopping list retry budget exhausted") from None
                    time.sleep(0.01)
                except Exception:
                    conn.rollback()
                    raise
        raise ShoppingConflictError("shopping list retry budget exhausted")

    def get_shopping_list(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_list_id: str,
    ) -> ShoppingListView:
        auth = self._authorize(context, household_id=None, operation="read")
        self._require_canonical_schema()
        with self._read_only_connect() as conn:
            shopping_list = self._get_list_by_id(conn, require_shopping_list_id(shopping_list_id))
            if shopping_list is None:
                raise ShoppingNotFoundError("shopping list not found")
            self._assert_list_in_scope(auth, shopping_list)
            return self._build_list_view(conn, shopping_list)

    def list_shopping_lists(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        household_id: str,
        *,
        week_start: str | None = None,
    ) -> tuple[ShoppingList, ...]:
        auth = self._authorize(context, household_id=household_id, operation="read")
        self._require_canonical_schema()
        params: list[object] = [auth.household_id]
        query = f"SELECT * FROM {SHOPPING_LISTS_TABLE} WHERE household_id = ?"
        if week_start is not None:
            query += " AND week_start = ?"
            params.append(require_monday_week_start(week_start))
        query += " ORDER BY week_start ASC, created_at ASC, id ASC"
        with self._read_only_connect() as conn:
            return tuple(self._row_to_list(row) for row in conn.execute(query, tuple(params)).fetchall())

    def get_current_shopping_list(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        household_id: str,
        *,
        week_start: str,
    ) -> ShoppingListView | None:
        auth = self._authorize(context, household_id=household_id, operation="read")
        self._require_canonical_schema()
        canonical_week_start = require_monday_week_start(week_start)
        with self._read_only_connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {SHOPPING_LISTS_TABLE}
                WHERE household_id = ? AND week_start = ? AND status = ?
                ORDER BY created_at ASC, id ASC
                """,
                (auth.household_id, canonical_week_start, ShoppingListStatus.ACTIVE.value),
            ).fetchall()
            if len(rows) > 1:
                raise ShoppingStateError("multiple active shopping lists")
            if not rows:
                return None
            return self._build_list_view(conn, self._row_to_list(rows[0]))

    def list_shopping_items(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_list_id: str,
    ) -> tuple[ShoppingItem, ...]:
        return self.get_shopping_list(context, shopping_list_id).items

    def activate_shopping_list(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_list_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ShoppingListView:
        return self._transition_list_status(
            context,
            shopping_list_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            target_status=ShoppingListStatus.ACTIVE,
        )

    def complete_shopping_list(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_list_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ShoppingListView:
        return self._transition_list_status(
            context,
            shopping_list_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            target_status=ShoppingListStatus.COMPLETED,
        )

    def archive_shopping_list(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_list_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ShoppingListView:
        return self._transition_list_status(
            context,
            shopping_list_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            target_status=ShoppingListStatus.ARCHIVED,
        )

    def add_manual_item(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_list_id: str,
        item: ManualShoppingItemInput,
        *,
        expected_list_version: int,
        idempotency_key: str,
    ) -> ShoppingListView:
        auth = self._authorize(context, household_id=None, operation="item_edit")
        self._require_canonical_schema()
        normalized_item = _normalized_manual_payload(item)
        list_id = require_shopping_list_id(shopping_list_id)
        expected_list_version = _normalize_version(expected_list_version, label="expected_list_version")
        normalized_key = _normalize_idempotency_key(idempotency_key)
        payload_hash = _payload_fingerprint(
            {
                "shopping_list_id": list_id,
                "expected_list_version": expected_list_version,
                "item": normalized_item,
            }
        )
        for attempt in range(_MAX_ID_REGENERATION_ATTEMPTS):
            with self._connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    auth = self._revalidate_shopping_actor_for_write(
                        conn,
                        context,
                        operation="item_edit",
                    )
                    shopping_list = self._get_list_by_id(conn, list_id)
                    if shopping_list is None:
                        raise ShoppingNotFoundError("shopping list not found")
                    self._assert_list_in_scope(auth, shopping_list)
                    self._assert_item_mutation_allowed(auth, shopping_list)
                    existing = self._resolve_idempotent_list(
                        conn=conn,
                        auth=auth,
                        operation=ShoppingIdempotencyOperation.ADD_MANUAL_ITEM,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                    )
                    if existing is not None:
                        conn.commit()
                        return self._build_list_view(conn, existing)
                    if shopping_list.version != expected_list_version:
                        raise ShoppingConflictError("shopping list version mismatch")
                    now = _sqlite_timestamp()
                    item_id = require_shopping_item_id(self._shopping_item_id_factory())
                    position = self._insert_position(
                        conn,
                        shopping_list_id=list_id,
                        requested_position=normalized_item["position"],
                    )
                    dedup_fingerprint = _dedup_fingerprint(
                        normalized_name=str(normalized_item["normalized_name"]),
                        quantity_unit_normalized=normalized_item["quantity_unit_normalized"],
                        category=normalized_item["category"],
                        origin=ShoppingItemOrigin.MANUAL,
                        override_state=ShoppingItemOverrideState.NONE,
                        source_lineage_token=None,
                        normalization_version=_NORMALIZATION_VERSION,
                    )
                    conn.execute(
                        f"""
                        INSERT INTO {SHOPPING_ITEMS_TABLE}
                            (id, shopping_list_id, household_id, normalized_name, display_name, quantity_value,
                             quantity_unit_normalized, quantity_unit_display, category, position, checked_state,
                             origin, override_state, source_menu_entry_id, normalization_version, dedup_fingerprint,
                             created_at, updated_at, version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, NULL, ?, ?, ?, ?, 1)
                        """,
                        (
                            item_id,
                            list_id,
                            shopping_list.household_id,
                            normalized_item["normalized_name"],
                            normalized_item["display_name"],
                            normalized_item["quantity_value"],
                            normalized_item["quantity_unit_normalized"].value,
                            normalized_item["quantity_unit_display"],
                            normalized_item["category"],
                            position,
                            ShoppingItemOrigin.MANUAL.value,
                            ShoppingItemOverrideState.NONE.value,
                            _NORMALIZATION_VERSION,
                            dedup_fingerprint,
                            now,
                            now,
                        ),
                    )
                    reordered_ids = self._ordered_item_ids_with_insert(
                        conn,
                        shopping_list_id=list_id,
                        inserted_item_id=item_id,
                        requested_position=normalized_item["position"],
                    )
                    self._rewrite_positions(conn, shopping_list_id=list_id, ordered_item_ids=reordered_ids)
                    self._bump_list(conn, list_id, now)
                    self._store_idempotency(
                        conn=conn,
                        auth=auth,
                        operation=ShoppingIdempotencyOperation.ADD_MANUAL_ITEM,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                        shopping_list_id=list_id,
                        shopping_item_id=item_id,
                    )
                    updated = self._get_list_by_id(conn, list_id)
                    if updated is None:
                        raise ShoppingStateError("manual item insertion failed")
                    conn.commit()
                    return self._build_list_view(conn, updated)
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    if "UNIQUE" not in str(exc).upper():
                        raise ShoppingConflictError("manual item conflict") from None
                    if attempt == _MAX_ID_REGENERATION_ATTEMPTS - 1:
                        raise ShoppingConflictError("manual item retry budget exhausted") from None
                    time.sleep(0.01)
                except Exception:
                    conn.rollback()
                    raise
        raise ShoppingConflictError("manual item retry budget exhausted")

    def update_item(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_item_id: str,
        *,
        expected_list_version: int,
        idempotency_key: str,
        display_name: object = _UNSET,
        quantity_value: object = _UNSET,
        quantity_unit_normalized: object = _UNSET,
        quantity_unit_display: object = _UNSET,
        category: object = _UNSET,
        position: object = _UNSET,
    ) -> ShoppingListView:
        auth = self._authorize(context, household_id=None, operation="item_edit")
        self._require_canonical_schema()
        item_id = require_shopping_item_id(shopping_item_id)
        expected_list_version = _normalize_version(expected_list_version, label="expected_list_version")
        normalized_key = _normalize_idempotency_key(idempotency_key)
        payload_hash = _payload_fingerprint(
            {
                "shopping_item_id": item_id,
                "expected_list_version": expected_list_version,
                "display_name": None if display_name is _UNSET else display_name,
                "quantity_value": None if quantity_value is _UNSET else quantity_value,
                "quantity_unit_normalized": None if quantity_unit_normalized is _UNSET else quantity_unit_normalized,
                "quantity_unit_display": None if quantity_unit_display is _UNSET else quantity_unit_display,
                "category": None if category is _UNSET else category,
                "position": None if position is _UNSET else position,
            }
        )
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                auth = self._revalidate_shopping_actor_for_write(
                    conn,
                    context,
                    operation="item_edit",
                )
                item_row = self._get_item_by_id(conn, item_id)
                if item_row is None:
                    raise ShoppingNotFoundError("shopping item not found")
                shopping_list = self._get_list_by_id(conn, item_row.shopping_list_id)
                if shopping_list is None:
                    raise ShoppingStateError("shopping item references missing list")
                self._assert_list_in_scope(auth, shopping_list)
                self._assert_item_mutation_allowed(auth, shopping_list)
                existing = self._resolve_idempotent_list(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.UPDATE_ITEM,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                )
                if existing is not None:
                    conn.commit()
                    return self._build_list_view(conn, existing)
                if shopping_list.version != expected_list_version:
                    raise ShoppingConflictError("shopping list version mismatch")
                next_display = item_row.display_name if display_name is _UNSET else _normalize_display_name(str(display_name))
                next_unit = item_row.quantity_unit_normalized if quantity_unit_normalized is _UNSET else normalize_shopping_unit(quantity_unit_normalized)
                next_quantity = item_row.quantity_value if quantity_value is _UNSET else normalize_quantity_value(quantity_value)
                if not quantity_contract_is_valid(next_quantity, next_unit):
                    raise ShoppingValidationError("invalid quantity/unit contract")
                if quantity_unit_display is _UNSET:
                    next_display_unit = _normalize_display_unit(next_unit, item_row.quantity_unit_display)
                else:
                    next_display_unit = _normalize_display_unit(next_unit, None if quantity_unit_display is None else str(quantity_unit_display))
                next_category = item_row.category if category is _UNSET else _normalize_category(None if category is None else str(category))
                next_position = item_row.position if position is _UNSET else _normalize_positive_int(position, label="position")
                next_origin = item_row.origin
                next_override_state = item_row.override_state
                if item_row.origin is ShoppingItemOrigin.MENU_GENERATED:
                    next_override_state = ShoppingItemOverrideState.MANUALIZED
                next_normalized_name = _normalize_identity(next_display)
                next_fingerprint = _dedup_fingerprint(
                    normalized_name=next_normalized_name,
                    quantity_unit_normalized=next_unit,
                    category=next_category,
                    origin=next_origin,
                    override_state=next_override_state,
                    source_lineage_token=item_row.source_menu_entry_id,
                    normalization_version=_NORMALIZATION_VERSION,
                )
                if (
                    item_row.display_name == next_display
                    and item_row.quantity_value == next_quantity
                    and item_row.quantity_unit_normalized is next_unit
                    and item_row.quantity_unit_display == next_display_unit
                    and item_row.category == next_category
                    and item_row.position == next_position
                    and item_row.override_state is next_override_state
                    and item_row.normalized_name == next_normalized_name
                    and item_row.dedup_fingerprint == next_fingerprint
                ):
                    self._store_idempotency(
                        conn=conn,
                        auth=auth,
                        operation=ShoppingIdempotencyOperation.UPDATE_ITEM,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                        shopping_list_id=shopping_list.id,
                        shopping_item_id=item_row.id,
                    )
                    conn.commit()
                    return self._build_list_view(conn, shopping_list)
                now = _sqlite_timestamp()
                conn.execute(
                    f"""
                    UPDATE {SHOPPING_ITEMS_TABLE}
                    SET normalized_name = ?,
                        display_name = ?,
                        quantity_value = ?,
                        quantity_unit_normalized = ?,
                        quantity_unit_display = ?,
                        category = ?,
                        position = ?,
                        override_state = ?,
                        normalization_version = ?,
                        dedup_fingerprint = ?,
                        updated_at = ?,
                        version = version + 1
                    WHERE id = ?
                    """,
                    (
                        next_normalized_name,
                        next_display,
                        next_quantity,
                        next_unit.value,
                        next_display_unit,
                        next_category,
                        item_row.position,
                        next_override_state.value,
                        _NORMALIZATION_VERSION,
                        next_fingerprint,
                        now,
                        item_row.id,
                    ),
                )
                if next_position != item_row.position:
                    reordered_ids = self._ordered_item_ids_with_move(
                        conn,
                        shopping_list_id=shopping_list.id,
                        moving_item_id=item_row.id,
                        requested_position=next_position,
                    )
                    self._rewrite_positions(conn, shopping_list_id=shopping_list.id, ordered_item_ids=reordered_ids)
                else:
                    self._normalize_positions(conn, shopping_list_id=shopping_list.id)
                self._bump_list(conn, shopping_list.id, now)
                self._store_idempotency(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.UPDATE_ITEM,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                    shopping_list_id=shopping_list.id,
                    shopping_item_id=item_row.id,
                )
                updated_list = self._get_list_by_id(conn, shopping_list.id)
                if updated_list is None:
                    raise ShoppingStateError("shopping item update failed")
                conn.commit()
                return self._build_list_view(conn, updated_list)
            except Exception:
                conn.rollback()
                raise

    def set_item_checked(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_item_id: str,
        desired_state: bool,
        *,
        expected_list_version: int | None = None,
        expected_item_version: int | None = None,
        idempotency_key: str,
    ) -> ShoppingListView:
        auth = self._authorize(context, household_id=None, operation="item_edit")
        self._require_canonical_schema()
        item_id = require_shopping_item_id(shopping_item_id)
        desired = _normalize_checked_state(desired_state)
        if (expected_list_version is None) == (expected_item_version is None):
            raise ShoppingValidationError("exactly one expected version is required")
        normalized_list_version = (
            None
            if expected_list_version is None
            else _normalize_version(expected_list_version, label="expected_list_version")
        )
        normalized_item_version = (
            None
            if expected_item_version is None
            else _normalize_version(expected_item_version, label="expected_item_version")
        )
        normalized_key = _normalize_idempotency_key(idempotency_key)
        payload: dict[str, object] = {
            "shopping_item_id": item_id,
            "desired_state": desired,
        }
        if normalized_list_version is not None:
            payload["expected_list_version"] = normalized_list_version
        else:
            payload["expected_item_version"] = normalized_item_version
        payload_hash = _payload_fingerprint(payload)
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                auth = self._revalidate_shopping_actor_for_write(
                    conn,
                    context,
                    operation="item_edit",
                )
                item_row = self._get_item_by_id(conn, item_id)
                if item_row is None:
                    raise ShoppingNotFoundError("shopping item not found")
                shopping_list = self._get_list_by_id(conn, item_row.shopping_list_id)
                if shopping_list is None:
                    raise ShoppingStateError("shopping item references missing list")
                self._assert_list_in_scope(auth, shopping_list)
                self._assert_item_mutation_allowed(auth, shopping_list)
                existing = self._resolve_idempotent_list(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.SET_ITEM_CHECKED,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                )
                if existing is not None:
                    conn.commit()
                    return self._build_list_view(conn, existing)
                if normalized_list_version is not None and shopping_list.version != normalized_list_version:
                    raise ShoppingConflictError("shopping list version mismatch")
                if normalized_item_version is not None and item_row.version != normalized_item_version:
                    raise ShoppingConflictError("shopping item version mismatch")
                if item_row.checked_state is desired:
                    self._store_idempotency(
                        conn=conn,
                        auth=auth,
                        operation=ShoppingIdempotencyOperation.SET_ITEM_CHECKED,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                        shopping_list_id=shopping_list.id,
                        shopping_item_id=item_row.id,
                    )
                    conn.commit()
                    return self._build_list_view(conn, shopping_list)
                now = _sqlite_timestamp()
                conn.execute(
                    f"""
                    UPDATE {SHOPPING_ITEMS_TABLE}
                    SET checked_state = ?, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (1 if desired else 0, now, item_row.id),
                )
                self._bump_list(conn, shopping_list.id, now)
                self._store_idempotency(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.SET_ITEM_CHECKED,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                    shopping_list_id=shopping_list.id,
                    shopping_item_id=item_row.id,
                )
                updated_list = self._get_list_by_id(conn, shopping_list.id)
                if updated_list is None:
                    raise ShoppingStateError("shopping item check update failed")
                conn.commit()
                return self._build_list_view(conn, updated_list)
            except Exception:
                conn.rollback()
                raise

    def delete_item(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_item_id: str,
        *,
        expected_item_version: int,
        idempotency_key: str,
    ) -> ShoppingListView:
        auth = self._authorize(context, household_id=None, operation="item_edit")
        self._require_canonical_schema()
        item_id = require_shopping_item_id(shopping_item_id)
        expected_version = _normalize_version(expected_item_version, label="expected_item_version")
        normalized_key = _scoped_idempotency_key("delete_item", idempotency_key)
        payload_hash = _payload_fingerprint(
            {"shopping_item_id": item_id, "expected_item_version": expected_version}
        )
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                auth = self._revalidate_shopping_actor_for_write(
                    conn,
                    context,
                    operation="item_edit",
                )
                existing = self._resolve_idempotent_list(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.UPDATE_ITEM,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                )
                if existing is not None:
                    conn.commit()
                    return self._build_list_view(conn, existing)
                item_row = self._get_item_by_id(conn, item_id)
                if item_row is None:
                    raise ShoppingNotFoundError("shopping item not found")
                shopping_list = self._get_list_by_id(conn, item_row.shopping_list_id)
                if shopping_list is None:
                    raise ShoppingStateError("shopping item references missing list")
                self._assert_list_in_scope(auth, shopping_list)
                self._assert_item_mutation_allowed(auth, shopping_list)
                if item_row.version != expected_version:
                    raise ShoppingConflictError("shopping item version mismatch")
                conn.execute(
                    f"UPDATE {SHOPPING_IDEMPOTENCY_TABLE} SET shopping_item_id = NULL WHERE shopping_item_id = ?",
                    (item_id,),
                )
                conn.execute(f"DELETE FROM {SHOPPING_ITEMS_TABLE} WHERE id = ?", (item_id,))
                self._normalize_positions(conn, shopping_list_id=shopping_list.id)
                self._bump_list(conn, shopping_list.id, _sqlite_timestamp())
                self._store_idempotency(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.UPDATE_ITEM,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                    shopping_list_id=shopping_list.id,
                    shopping_item_id=None,
                )
                updated = self._get_list_by_id(conn, shopping_list.id)
                if updated is None:
                    raise ShoppingStateError("shopping item deletion failed")
                conn.commit()
                return self._build_list_view(conn, updated)
            except Exception:
                conn.rollback()
                raise

    def clear_shopping_list(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_list_id: str,
        *,
        clear_mode: str,
        expected_list_version: int,
        idempotency_key: str,
    ) -> ShoppingListView:
        auth = self._authorize(context, household_id=None, operation="item_edit")
        self._require_canonical_schema()
        list_id = require_shopping_list_id(shopping_list_id)
        if str(clear_mode) != "all_items":
            raise ShoppingValidationError("unsupported clear mode")
        expected_version = _normalize_version(expected_list_version, label="expected_list_version")
        normalized_key = _scoped_idempotency_key("clear_all_items", idempotency_key)
        payload_hash = _payload_fingerprint(
            {
                "shopping_list_id": list_id,
                "clear_mode": "all_items",
                "expected_list_version": expected_version,
            }
        )
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                auth = self._revalidate_shopping_actor_for_write(
                    conn,
                    context,
                    operation="item_edit",
                )
                shopping_list = self._get_list_by_id(conn, list_id)
                if shopping_list is None:
                    raise ShoppingNotFoundError("shopping list not found")
                self._assert_list_in_scope(auth, shopping_list)
                self._assert_item_mutation_allowed(auth, shopping_list)
                existing = self._resolve_idempotent_list(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.UPDATE_ITEM,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                )
                if existing is not None:
                    conn.commit()
                    return self._build_list_view(conn, existing)
                if shopping_list.version != expected_version:
                    raise ShoppingConflictError("shopping list version mismatch")
                item_ids = tuple(item.id for item in self._list_items_for_list(conn, list_id))
                if item_ids:
                    placeholders = ",".join("?" for _ in item_ids)
                    conn.execute(
                        f"UPDATE {SHOPPING_IDEMPOTENCY_TABLE} SET shopping_item_id = NULL WHERE shopping_item_id IN ({placeholders})",
                        item_ids,
                    )
                    conn.execute(f"DELETE FROM {SHOPPING_ITEMS_TABLE} WHERE shopping_list_id = ?", (list_id,))
                    self._bump_list(conn, list_id, _sqlite_timestamp())
                self._store_idempotency(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.UPDATE_ITEM,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                    shopping_list_id=list_id,
                    shopping_item_id=None,
                )
                updated = self._get_list_by_id(conn, list_id)
                if updated is None:
                    raise ShoppingStateError("shopping list clear failed")
                conn.commit()
                return self._build_list_view(conn, updated)
            except Exception:
                conn.rollback()
                raise

    def generate_shopping_list_from_weekly_menu(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        week_start: str,
        *,
        expected_list_version: int | None,
        idempotency_key: str,
    ) -> ShoppingListView:
        """Atomically derive an active list; deleted generated items may return."""
        self._authorize(context, household_id=None, operation="regenerate")
        self._require_canonical_schema()
        canonical_week_start = require_monday_week_start(week_start)
        expected_version = (
            None
            if expected_list_version is None
            else _normalize_version(
                expected_list_version,
                label="expected_list_version",
            )
        )
        normalized_key = _normalize_idempotency_key(idempotency_key)
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                auth = self._revalidate_shopping_actor_for_write(
                    conn,
                    context,
                    operation="regenerate",
                )
                self._derivation_fault("after_authorization")
                source = conn.execute(
                    f"""
                    SELECT r.*, s.week_start
                    FROM {WEEKLY_MENU_REVISIONS_TABLE} r
                    JOIN {WEEKLY_MENU_SERIES_TABLE} s ON s.id = r.series_id
                    WHERE r.household_id = ?
                      AND s.week_start = ?
                      AND r.status = ?
                    LIMIT 1
                    """,
                    (
                        auth.household_id,
                        canonical_week_start,
                        WeeklyMenuRevisionStatus.PUBLISHED.value,
                    ),
                ).fetchone()
                if source is None:
                    raise ShoppingNotFoundError("published weekly menu not found")
                entry_rows = conn.execute(
                    f"""
                    SELECT id, servings
                    FROM {WEEKLY_MENU_ENTRIES_TABLE}
                    WHERE menu_id = ? AND household_id = ?
                    ORDER BY local_date ASC, meal_slot ASC, position ASC, id ASC
                    """,
                    (source["id"], auth.household_id),
                ).fetchall()
                if not entry_rows:
                    raise ShoppingStateError("published weekly menu has no entries")
                ingredient_rows = conn.execute(
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
                    ORDER BY e.local_date ASC, e.meal_slot ASC, e.position ASC,
                             e.id ASC, i.position ASC, i.id ASC
                    """,
                    (source["id"], auth.household_id),
                ).fetchall()
                ingredient_entry_ids = {
                    str(row["source_menu_entry_id"]) for row in ingredient_rows
                }
                if any(str(row["id"]) not in ingredient_entry_ids for row in entry_rows):
                    raise ShoppingStateError(
                        "published weekly menu has incomplete ingredient snapshots"
                    )
                self._derivation_fault("after_source_read")
                derived_items = _derive_weekly_ingredient_rows(ingredient_rows)
                if not derived_items:
                    raise ShoppingStateError("published weekly menu has no ingredients")
                self._derivation_fault("after_validation")
                self._derivation_fault("after_aggregation")
                source_fingerprint = _payload_fingerprint(
                    {
                        "source_menu_id": str(source["id"]),
                        "source_revision_number": int(source["revision_number"]),
                        "source_revision_version": int(source["version"]),
                        "items": [
                            {
                                "fingerprint": item.dedup_fingerprint,
                                "quantity": item.quantity_value,
                                "unit": item.quantity_unit.value,
                            }
                            for item in derived_items
                        ],
                    }
                )
                payload_hash = _payload_fingerprint(
                    {
                        "week_start": canonical_week_start,
                        "expected_list_version": expected_version,
                        "source_fingerprint": source_fingerprint,
                    }
                )
                shopping_list_row = conn.execute(
                    f"""
                    SELECT * FROM {SHOPPING_LISTS_TABLE}
                    WHERE household_id = ? AND week_start = ? AND status = ?
                    LIMIT 1
                    """,
                    (
                        auth.household_id,
                        canonical_week_start,
                        ShoppingListStatus.ACTIVE.value,
                    ),
                ).fetchone()
                existing = self._resolve_idempotent_list(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.REGENERATE_GENERATED_ITEMS,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                )
                if existing is not None:
                    conn.commit()
                    return self._build_list_view(conn, existing)
                now = _sqlite_timestamp()
                if shopping_list_row is None:
                    if expected_version is not None:
                        raise ShoppingConflictError("shopping list version mismatch")
                    list_id = require_shopping_list_id(self._shopping_list_id_factory())
                    conn.execute(
                        f"""
                        INSERT INTO {SHOPPING_LISTS_TABLE}
                            (id, household_id, week_start, source_menu_id,
                             source_menu_revision, status, created_by_member_id,
                             created_at, updated_at, completed_at, archived_at, version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1)
                        """,
                        (
                            list_id,
                            auth.household_id,
                            canonical_week_start,
                            str(source["id"]),
                            int(source["revision_number"]),
                            ShoppingListStatus.ACTIVE.value,
                            auth.household_member_id,
                            now,
                            now,
                        ),
                    )
                    shopping_list = self._get_list_by_id(conn, list_id)
                    if shopping_list is None:
                        raise ShoppingStateError("shopping list creation failed")
                else:
                    shopping_list = self._row_to_list(shopping_list_row)
                    self._assert_list_in_scope(auth, shopping_list)
                    self._assert_regeneration_allowed(auth, shopping_list)
                    if expected_version is None or shopping_list.version != expected_version:
                        raise ShoppingConflictError("shopping list version mismatch")

                existing_items = self._list_items_for_list(conn, shopping_list.id)
                mutable_generated = [
                    item
                    for item in existing_items
                    if item.origin is ShoppingItemOrigin.MENU_GENERATED
                    and item.override_state is ShoppingItemOverrideState.NONE
                ]
                checked_by_fingerprint = {
                    item.dedup_fingerprint: item.checked_state
                    for item in mutable_generated
                }
                next_fingerprints = {item.dedup_fingerprint for item in derived_items}
                for item in mutable_generated:
                    if item.checked_state and item.dedup_fingerprint not in next_fingerprints:
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
                            f"UPDATE {SHOPPING_IDEMPOTENCY_TABLE} "
                            "SET shopping_item_id = NULL WHERE shopping_item_id = ?",
                            (item.id,),
                        )
                        conn.execute(
                            f"DELETE FROM {SHOPPING_ITEMS_TABLE} WHERE id = ?",
                            (item.id,),
                        )
                self._derivation_fault("after_generated_deletion")

                next_position = 1000000 + len(existing_items)
                for index, item in enumerate(derived_items):
                    item_id = require_shopping_item_id(self._shopping_item_id_factory())
                    conn.execute(
                        f"""
                        INSERT INTO {SHOPPING_ITEMS_TABLE}
                            (id, shopping_list_id, household_id, normalized_name,
                             display_name, quantity_value, quantity_unit_normalized,
                             quantity_unit_display, category, position, checked_state,
                             origin, override_state, source_menu_entry_id,
                             normalization_version, dedup_fingerprint, created_at,
                             updated_at, version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            item_id,
                            shopping_list.id,
                            auth.household_id,
                            item.normalized_name,
                            item.display_name,
                            item.quantity_value,
                            item.quantity_unit.value,
                            item.quantity_unit.value,
                            next_position,
                            int(checked_by_fingerprint.get(item.dedup_fingerprint, False)),
                            ShoppingItemOrigin.MENU_GENERATED.value,
                            ShoppingItemOverrideState.NONE.value,
                            item.source_menu_entry_id,
                            _NORMALIZATION_VERSION,
                            item.dedup_fingerprint,
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
                                require_canonical_uuid4(
                                    self._contribution_id_factory()
                                ),
                                item_id,
                                contribution.source_menu_entry_id,
                                contribution.source_ingredient_id,
                                contribution.scaled_quantity_value,
                                item.quantity_unit.value,
                                now,
                            ),
                        )
                    if index == 0:
                        self._derivation_fault("after_first_generated_insert")
                self._derivation_fault("after_generated_mutation")
                self._normalize_positions(conn, shopping_list_id=shopping_list.id)
                if shopping_list_row is not None:
                    conn.execute(
                        f"""
                        UPDATE {SHOPPING_LISTS_TABLE}
                        SET source_menu_id = ?, source_menu_revision = ?,
                            updated_at = ?, version = version + 1
                        WHERE id = ?
                        """,
                        (
                            str(source["id"]),
                            int(source["revision_number"]),
                            now,
                            shopping_list.id,
                        ),
                    )
                self._derivation_fault("after_list_version_update")
                self._store_idempotency(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.REGENERATE_GENERATED_ITEMS,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                    shopping_list_id=shopping_list.id,
                    shopping_item_id=None,
                )
                self._derivation_fault("after_idempotency_write")
                updated = self._get_list_by_id(conn, shopping_list.id)
                if updated is None:
                    raise ShoppingStateError("shopping derivation failed")
                self._derivation_fault("before_commit")
                conn.commit()
                return self._build_list_view(conn, updated)
            except Exception:
                conn.rollback()
                raise

    def replace_or_regenerate_generated_items(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_list_id: str,
        items: Sequence[GeneratedShoppingItemInput],
        *,
        expected_version: int,
        idempotency_key: str,
    ) -> ShoppingListView:
        auth = self._authorize(context, household_id=None, operation="regenerate")
        self._require_canonical_schema()
        list_id = require_shopping_list_id(shopping_list_id)
        expected_version = _normalize_version(expected_version, label="expected_version")
        normalized_key = _normalize_idempotency_key(idempotency_key)
        aggregated_items = _aggregate_generated_payloads(items)
        payload_hash = _payload_fingerprint(
            {
                "shopping_list_id": list_id,
                "expected_version": expected_version,
                "items": [_serialize_generated_payload(item) for item in aggregated_items],
            }
        )
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                auth = self._revalidate_shopping_actor_for_write(
                    conn,
                    context,
                    operation="regenerate",
                )
                shopping_list = self._get_list_by_id(conn, list_id)
                if shopping_list is None:
                    raise ShoppingNotFoundError("shopping list not found")
                self._assert_list_in_scope(auth, shopping_list)
                self._assert_regeneration_allowed(auth, shopping_list)
                existing = self._resolve_idempotent_list(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.REGENERATE_GENERATED_ITEMS,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                )
                if existing is not None:
                    conn.commit()
                    return self._build_list_view(conn, existing)
                if shopping_list.version != expected_version:
                    raise ShoppingConflictError("shopping list version mismatch")
                self._validate_generated_source_inputs(
                    conn,
                    shopping_list=shopping_list,
                    items=aggregated_items,
                )
                existing_items = list(self._list_items_for_list(conn, shopping_list.id))
                manual_items = [item for item in existing_items if item.origin is ShoppingItemOrigin.MANUAL]
                protected_overrides = [
                    item
                    for item in existing_items
                    if item.origin is ShoppingItemOrigin.MENU_GENERATED
                    and item.override_state is ShoppingItemOverrideState.MANUALIZED
                ]
                mutable_generated = {
                    item.dedup_fingerprint: item
                    for item in existing_items
                    if item.origin is ShoppingItemOrigin.MENU_GENERATED
                    and item.override_state is ShoppingItemOverrideState.NONE
                }
                matched_ids: set[str] = set()
                semantic_delta = False
                now = _sqlite_timestamp()
                next_generated_position = 1000000 + len(existing_items)
                for payload in aggregated_items:
                    fingerprint = str(payload["dedup_fingerprint"])
                    matched = mutable_generated.get(fingerprint)
                    if matched is None:
                        item_id = require_shopping_item_id(self._shopping_item_id_factory())
                        conn.execute(
                            f"""
                            INSERT INTO {SHOPPING_ITEMS_TABLE}
                                (id, shopping_list_id, household_id, normalized_name, display_name, quantity_value,
                                 quantity_unit_normalized, quantity_unit_display, category, position, checked_state,
                                 origin, override_state, source_menu_entry_id, normalization_version, dedup_fingerprint,
                                 created_at, updated_at, version)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 1)
                            """,
                            (
                                item_id,
                                shopping_list.id,
                                shopping_list.household_id,
                                payload["normalized_name"],
                                payload["display_name"],
                                payload["quantity_value"],
                                payload["quantity_unit_normalized"].value,
                                payload["quantity_unit_display"],
                                payload["category"],
                                next_generated_position,
                                ShoppingItemOrigin.MENU_GENERATED.value,
                                ShoppingItemOverrideState.NONE.value,
                                payload["source_menu_entry_id"],
                                _NORMALIZATION_VERSION,
                                fingerprint,
                                now,
                                now,
                            ),
                        )
                        next_generated_position += 1
                        semantic_delta = True
                        continue
                    matched_ids.add(matched.id)
                    if (
                        matched.display_name == payload["display_name"]
                        and matched.quantity_value == payload["quantity_value"]
                        and matched.quantity_unit_normalized is payload["quantity_unit_normalized"]
                        and matched.quantity_unit_display == payload["quantity_unit_display"]
                        and matched.category == payload["category"]
                        and matched.source_menu_entry_id == payload["source_menu_entry_id"]
                    ):
                        continue
                    conn.execute(
                        f"""
                        UPDATE {SHOPPING_ITEMS_TABLE}
                        SET normalized_name = ?,
                            display_name = ?,
                            quantity_value = ?,
                            quantity_unit_normalized = ?,
                            quantity_unit_display = ?,
                            category = ?,
                            source_menu_entry_id = ?,
                            normalization_version = ?,
                            updated_at = ?,
                            version = version + 1
                        WHERE id = ?
                        """,
                        (
                            payload["normalized_name"],
                            payload["display_name"],
                            payload["quantity_value"],
                            payload["quantity_unit_normalized"].value,
                            payload["quantity_unit_display"],
                            payload["category"],
                            payload["source_menu_entry_id"],
                            _NORMALIZATION_VERSION,
                            now,
                            matched.id,
                        ),
                    )
                    semantic_delta = True
                for existing_generated in mutable_generated.values():
                    if existing_generated.id in matched_ids:
                        continue
                    if existing_generated.checked_state:
                        conn.execute(
                            f"""
                            UPDATE {SHOPPING_ITEMS_TABLE}
                            SET override_state = ?, updated_at = ?, version = version + 1
                            WHERE id = ?
                            """,
                            (ShoppingItemOverrideState.MANUALIZED.value, now, existing_generated.id),
                        )
                    else:
                        conn.execute(f"DELETE FROM {SHOPPING_ITEMS_TABLE} WHERE id = ?", (existing_generated.id,))
                    semantic_delta = True
                self._normalize_positions(conn, shopping_list_id=shopping_list.id)
                if semantic_delta:
                    self._bump_list(conn, shopping_list.id, now)
                self._store_idempotency(
                    conn=conn,
                    auth=auth,
                    operation=ShoppingIdempotencyOperation.REGENERATE_GENERATED_ITEMS,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                    shopping_list_id=shopping_list.id,
                    shopping_item_id=None,
                )
                updated_list = self._get_list_by_id(conn, shopping_list.id)
                if updated_list is None:
                    raise ShoppingStateError("shopping regeneration failed")
                conn.commit()
                return self._build_list_view(conn, updated_list)
            except Exception:
                conn.rollback()
                raise

    def _transition_list_status(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        shopping_list_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        target_status: ShoppingListStatus,
    ) -> ShoppingListView:
        auth = self._authorize(context, household_id=None, operation="list_lifecycle")
        self._require_canonical_schema()
        list_id = require_shopping_list_id(shopping_list_id)
        expected_version = _normalize_version(expected_version, label="expected_version")
        normalized_key = _normalize_idempotency_key(idempotency_key)
        payload_hash = _payload_fingerprint(
            {
                "shopping_list_id": list_id,
                "expected_version": expected_version,
                "target_status": target_status.value,
            }
        )
        operation = {
            ShoppingListStatus.ACTIVE: ShoppingIdempotencyOperation.ACTIVATE_LIST,
            ShoppingListStatus.COMPLETED: ShoppingIdempotencyOperation.COMPLETE_LIST,
            ShoppingListStatus.ARCHIVED: ShoppingIdempotencyOperation.ARCHIVE_LIST,
        }[target_status]
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                auth = self._revalidate_shopping_actor_for_write(
                    conn,
                    context,
                    operation="list_lifecycle",
                )
                shopping_list = self._get_list_by_id(conn, list_id)
                if shopping_list is None:
                    raise ShoppingNotFoundError("shopping list not found")
                self._assert_list_in_scope(auth, shopping_list)
                self._assert_list_lifecycle_allowed(auth, shopping_list, target_status)
                existing = self._resolve_idempotent_list(
                    conn=conn,
                    auth=auth,
                    operation=operation,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                )
                if existing is not None:
                    conn.commit()
                    return self._build_list_view(conn, existing)
                if shopping_list.version != expected_version:
                    raise ShoppingConflictError("shopping list version mismatch")
                if shopping_list.status is target_status:
                    self._store_idempotency(
                        conn=conn,
                        auth=auth,
                        operation=operation,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                        shopping_list_id=shopping_list.id,
                        shopping_item_id=None,
                    )
                    conn.commit()
                    return self._build_list_view(conn, shopping_list)
                allowed = {
                    ShoppingListStatus.ACTIVE: {ShoppingListStatus.DRAFT},
                    ShoppingListStatus.COMPLETED: {ShoppingListStatus.ACTIVE},
                    ShoppingListStatus.ARCHIVED: {
                        ShoppingListStatus.DRAFT,
                        ShoppingListStatus.ACTIVE,
                        ShoppingListStatus.COMPLETED,
                    },
                }[target_status]
                if shopping_list.status not in allowed:
                    raise ShoppingStateError("invalid shopping list status transition")
                now = _sqlite_timestamp()
                completed_at = shopping_list.completed_at
                archived_at = shopping_list.archived_at
                if target_status is ShoppingListStatus.COMPLETED:
                    completed_at = now
                if target_status is ShoppingListStatus.ARCHIVED:
                    archived_at = now
                    if shopping_list.status is ShoppingListStatus.ACTIVE:
                        completed_at = shopping_list.completed_at
                conn.execute(
                    f"""
                    UPDATE {SHOPPING_LISTS_TABLE}
                    SET status = ?, completed_at = ?, archived_at = ?, updated_at = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (
                        target_status.value,
                        completed_at,
                        archived_at,
                        now,
                        shopping_list.id,
                    ),
                )
                self._store_idempotency(
                    conn=conn,
                    auth=auth,
                    operation=operation,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                    shopping_list_id=shopping_list.id,
                    shopping_item_id=None,
                )
                updated = self._get_list_by_id(conn, shopping_list.id)
                if updated is None:
                    raise ShoppingStateError("shopping list status transition failed")
                conn.commit()
                return self._build_list_view(conn, updated)
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if "UNIQUE" not in str(exc).upper():
                    raise ShoppingConflictError("shopping list status conflict") from None
                raise ShoppingConflictError("single active shopping list conflict") from None
            except Exception:
                conn.rollback()
                raise

    def _require_canonical_schema(self) -> None:
        state = self.schema_state()
        if state is ShoppingSchemaState.DEPENDENCY_MISSING:
            raise ShoppingSchemaError("shopping schema dependency missing")
        if state is ShoppingSchemaState.NOT_INITIALIZED:
            raise ShoppingSchemaError("shopping schema is not initialized")
        if state is ShoppingSchemaState.PARTIAL:
            raise ShoppingSchemaError("shopping schema is partial")
        if state is ShoppingSchemaState.INCOMPATIBLE:
            raise ShoppingSchemaError("shopping schema is incompatible")

    def _derivation_fault(self, phase: str) -> None:
        if self._derivation_fault_hook is not None:
            self._derivation_fault_hook(phase)

    def _revalidate_shopping_actor_for_write(
        self,
        conn: sqlite3.Connection,
        context: HouseholdContext | HouseholdAuthorizationContext,
        *,
        operation: str,
    ) -> HouseholdAuthorizationContext:
        expected = HouseholdAuthorizationContext.from_household_context(context)
        try:
            current_context = self._household_store._resolve_existing_actor_context_on_connection(
                conn,
                expected.actor_user_id,
            )
        except (
            HouseholdAccessError,
            HouseholdIntegrityError,
            HouseholdNotFoundError,
            HouseholdValidationError,
        ) as exc:
            raise ShoppingAccessError("shopping access denied") from exc
        current = HouseholdAuthorizationContext.from_household_context(current_context)
        if current.household_id != expected.household_id:
            raise ShoppingAccessError("shopping access denied")
        return self._authorize(
            current,
            household_id=expected.household_id,
            operation=operation,
        )

    def _authorize(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        *,
        household_id: str | None,
        operation: str,
    ) -> HouseholdAuthorizationContext:
        auth = HouseholdAuthorizationContext.from_household_context(context)
        _normalize_positive_int(auth.actor_user_id, label="actor")
        if auth.member_status is not HouseholdMemberStatus.ACTIVE:
            raise ShoppingAccessError("household member is not active")
        if auth.household_status is not HouseholdStatus.ACTIVE:
            raise ShoppingAccessError("household is not active")
        if household_id is not None and require_canonical_uuid4(household_id) != auth.household_id:
            raise ShoppingAccessError("household scope mismatch")
        if operation in {"list_lifecycle", "regenerate"} and auth.role not in (
            HouseholdRole.OWNER,
            HouseholdRole.ADULT_ADMIN,
        ):
            raise ShoppingAccessError("household member may not mutate shopping lifecycle")
        if operation == "item_edit" and auth.role not in (
            HouseholdRole.OWNER,
            HouseholdRole.ADULT_ADMIN,
            HouseholdRole.ADULT_MEMBER,
        ):
            raise ShoppingAccessError("household member may not edit shopping items")
        return auth

    def _assert_list_in_scope(self, auth: HouseholdAuthorizationContext, shopping_list: ShoppingList) -> None:
        if shopping_list.household_id != auth.household_id:
            raise ShoppingAccessError("shopping list out of household scope")

    def _assert_item_mutation_allowed(self, auth: HouseholdAuthorizationContext, shopping_list: ShoppingList) -> None:
        if shopping_list.status not in (ShoppingListStatus.DRAFT, ShoppingListStatus.ACTIVE):
            raise ShoppingStateError("shopping list is immutable")
        if auth.role not in (HouseholdRole.OWNER, HouseholdRole.ADULT_ADMIN, HouseholdRole.ADULT_MEMBER):
            raise ShoppingAccessError("household member may not edit shopping items")

    def _assert_regeneration_allowed(self, auth: HouseholdAuthorizationContext, shopping_list: ShoppingList) -> None:
        if shopping_list.status not in (ShoppingListStatus.DRAFT, ShoppingListStatus.ACTIVE):
            raise ShoppingStateError("shopping list is immutable")
        if auth.role not in (HouseholdRole.OWNER, HouseholdRole.ADULT_ADMIN):
            raise ShoppingAccessError("household member may not regenerate shopping items")

    def _assert_list_lifecycle_allowed(
        self,
        auth: HouseholdAuthorizationContext,
        shopping_list: ShoppingList,
        target_status: ShoppingListStatus,
    ) -> None:
        if auth.role not in (HouseholdRole.OWNER, HouseholdRole.ADULT_ADMIN):
            raise ShoppingAccessError("household member may not mutate shopping lifecycle")
        if shopping_list.status is ShoppingListStatus.ARCHIVED:
            raise ShoppingStateError("archived shopping list is immutable")
        if target_status is ShoppingListStatus.ARCHIVED and auth.role is HouseholdRole.ADULT_MEMBER:
            raise ShoppingAccessError("adult member may not archive shopping list")

    def _validate_source_menu_revision(
        self,
        conn: sqlite3.Connection,
        *,
        household_id: str,
        source_menu_id: str,
        week_start: str,
        allow_archived: bool,
    ) -> sqlite3.Row:
        row = conn.execute(
            f"""
            SELECT r.*, s.week_start
            FROM {WEEKLY_MENU_REVISIONS_TABLE} r
            JOIN {WEEKLY_MENU_SERIES_TABLE} s ON s.id = r.series_id
            WHERE r.id = ? AND r.household_id = ?
            LIMIT 1
            """,
            (source_menu_id, household_id),
        ).fetchone()
        if row is None:
            raise ShoppingAccessError("source menu revision not found")
        status = WeeklyMenuRevisionStatus(str(row["status"]))
        if status is WeeklyMenuRevisionStatus.DRAFT:
            raise ShoppingStateError("source menu revision is not immutable")
        if not allow_archived and status is WeeklyMenuRevisionStatus.ARCHIVED:
            raise ShoppingStateError("source menu revision is archived")
        if str(row["week_start"]) != week_start:
            raise ShoppingValidationError("shopping week does not match source menu week")
        return row

    def _validate_generated_source_inputs(
        self,
        conn: sqlite3.Connection,
        *,
        shopping_list: ShoppingList,
        items: Sequence[dict[str, object]],
    ) -> None:
        source_menu_id = shopping_list.source_menu_id
        if source_menu_id is None:
            for item in items:
                if item["source_menu_entry_id"] is not None:
                    raise ShoppingValidationError("standalone shopping list cannot reference menu entries")
            return
        self._validate_source_menu_revision(
            conn,
            household_id=shopping_list.household_id,
            source_menu_id=source_menu_id,
            week_start=shopping_list.week_start,
            allow_archived=True,
        )
        for item in items:
            source_entry_id = item["source_menu_entry_id"]
            if source_entry_id is None:
                continue
            row = conn.execute(
                f"""
                SELECT id, menu_id, household_id
                FROM {WEEKLY_MENU_ENTRIES_TABLE}
                WHERE id = ?
                LIMIT 1
                """,
                (source_entry_id,),
            ).fetchone()
            if row is None:
                raise ShoppingValidationError("source menu entry not found")
            if str(row["menu_id"]) != source_menu_id or str(row["household_id"]) != shopping_list.household_id:
                raise ShoppingAccessError("source menu entry scope mismatch")

    def _resolve_idempotent_list(
        self,
        *,
        conn: sqlite3.Connection,
        auth: HouseholdAuthorizationContext,
        operation: ShoppingIdempotencyOperation,
        idempotency_key: str,
        payload_hash: str,
    ) -> ShoppingList | None:
        row = conn.execute(
            f"""
            SELECT * FROM {SHOPPING_IDEMPOTENCY_TABLE}
            WHERE household_id = ? AND actor_member_id = ? AND operation = ? AND idempotency_key = ?
            LIMIT 1
            """,
            (auth.household_id, auth.household_member_id, operation.value, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if str(row["payload_fingerprint"]) != payload_hash:
            raise ShoppingConflictError("idempotency key replayed with different payload")
        shopping_list_id = str(row["shopping_list_id"]) if row["shopping_list_id"] is not None else None
        if not shopping_list_id:
            raise ShoppingStateError("shopping idempotency references missing list")
        shopping_list = self._get_list_by_id(conn, shopping_list_id)
        if shopping_list is None:
            raise ShoppingStateError("shopping idempotency references missing list")
        return shopping_list

    def _store_idempotency(
        self,
        *,
        conn: sqlite3.Connection,
        auth: HouseholdAuthorizationContext,
        operation: ShoppingIdempotencyOperation,
        idempotency_key: str,
        payload_hash: str,
        shopping_list_id: str | None,
        shopping_item_id: str | None,
    ) -> None:
        conn.execute(
            f"""
            INSERT INTO {SHOPPING_IDEMPOTENCY_TABLE}
                (id, household_id, actor_member_id, operation, idempotency_key, payload_fingerprint,
                 shopping_list_id, shopping_item_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                require_canonical_uuid4(self._idempotency_id_factory()),
                auth.household_id,
                auth.household_member_id,
                operation.value,
                idempotency_key,
                payload_hash,
                shopping_list_id,
                shopping_item_id,
                _sqlite_timestamp(),
            ),
        )

    def _normalize_positions(self, conn: sqlite3.Connection, *, shopping_list_id: str) -> None:
        ordered = list(self._list_items_for_list(conn, shopping_list_id))
        self._rewrite_positions(conn, shopping_list_id=shopping_list_id, ordered_item_ids=[item.id for item in ordered])

    def _rewrite_positions(
        self,
        conn: sqlite3.Connection,
        *,
        shopping_list_id: str,
        ordered_item_ids: Sequence[str],
    ) -> None:
        ordered_ids = [require_shopping_item_id(item_id) for item_id in ordered_item_ids]
        id_set = set(ordered_ids)
        if not ordered_ids:
            return
        current_rows = conn.execute(
            f"SELECT id, position FROM {SHOPPING_ITEMS_TABLE} WHERE shopping_list_id = ? ORDER BY position ASC, id ASC",
            (shopping_list_id,),
        ).fetchall()
        if len(current_rows) != len(ordered_ids):
            raise ShoppingStateError("shopping item ordering mismatch")
        current_ids = {str(row["id"]) for row in current_rows}
        if current_ids != id_set:
            raise ShoppingStateError("shopping item ordering mismatch")
        max_position = max(int(row["position"]) for row in current_rows)
        for index, row in enumerate(current_rows, start=1):
            conn.execute(
                f"UPDATE {SHOPPING_ITEMS_TABLE} SET position = ? WHERE id = ?",
                (max_position + index, str(row["id"])),
            )
        for index, item_id in enumerate(ordered_ids, start=1):
            conn.execute(
                f"UPDATE {SHOPPING_ITEMS_TABLE} SET position = ? WHERE id = ?",
                (index, item_id),
            )

    def _ordered_item_ids_with_insert(
        self,
        conn: sqlite3.Connection,
        *,
        shopping_list_id: str,
        inserted_item_id: str,
        requested_position: int | None,
    ) -> list[str]:
        normalized_item_id = require_shopping_item_id(inserted_item_id)
        ordered_ids = [item.id for item in self._list_items_for_list(conn, shopping_list_id) if item.id != normalized_item_id]
        insert_at = len(ordered_ids) if requested_position is None else min(max(requested_position - 1, 0), len(ordered_ids))
        ordered_ids.insert(insert_at, normalized_item_id)
        return ordered_ids

    def _ordered_item_ids_with_move(
        self,
        conn: sqlite3.Connection,
        *,
        shopping_list_id: str,
        moving_item_id: str,
        requested_position: int,
    ) -> list[str]:
        normalized_item_id = require_shopping_item_id(moving_item_id)
        ordered_ids = [item.id for item in self._list_items_for_list(conn, shopping_list_id)]
        if normalized_item_id not in ordered_ids:
            raise ShoppingStateError("shopping item ordering mismatch")
        ordered_ids.remove(normalized_item_id)
        insert_at = min(max(requested_position - 1, 0), len(ordered_ids))
        ordered_ids.insert(insert_at, normalized_item_id)
        return ordered_ids

    def _insert_position(
        self,
        conn: sqlite3.Connection,
        *,
        shopping_list_id: str,
        requested_position: int | None,
    ) -> int:
        current_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {SHOPPING_ITEMS_TABLE} WHERE shopping_list_id = ?",
                (shopping_list_id,),
            ).fetchone()[0]
        )
        if requested_position is not None and requested_position < 1:
            raise ShoppingValidationError("invalid position")
        return current_count + 1

    def _bump_list(self, conn: sqlite3.Connection, shopping_list_id: str, now: str) -> None:
        conn.execute(
            f"""
            UPDATE {SHOPPING_LISTS_TABLE}
            SET updated_at = ?, version = version + 1
            WHERE id = ?
            """,
            (now, shopping_list_id),
        )

    def _get_list_by_id(self, conn: sqlite3.Connection, shopping_list_id: str) -> ShoppingList | None:
        row = conn.execute(
            f"SELECT * FROM {SHOPPING_LISTS_TABLE} WHERE id = ? LIMIT 1",
            (shopping_list_id,),
        ).fetchone()
        return None if row is None else self._row_to_list(row)

    def _get_item_by_id(self, conn: sqlite3.Connection, shopping_item_id: str) -> ShoppingItem | None:
        row = conn.execute(
            f"SELECT * FROM {SHOPPING_ITEMS_TABLE} WHERE id = ? LIMIT 1",
            (shopping_item_id,),
        ).fetchone()
        return None if row is None else self._row_to_item(row)

    def _list_items_for_list(self, conn: sqlite3.Connection, shopping_list_id: str) -> tuple[ShoppingItem, ...]:
        rows = conn.execute(
            f"""
            SELECT * FROM {SHOPPING_ITEMS_TABLE}
            WHERE shopping_list_id = ?
            ORDER BY position ASC, created_at ASC, id ASC
            """,
            (shopping_list_id,),
        ).fetchall()
        return tuple(self._row_to_item(row) for row in rows)

    def _build_list_view(self, conn: sqlite3.Connection, shopping_list: ShoppingList) -> ShoppingListView:
        return ShoppingListView(
            shopping_list=shopping_list,
            items=self._list_items_for_list(conn, shopping_list.id),
        )

    def _row_to_list(self, row: sqlite3.Row) -> ShoppingList:
        try:
            shopping_list_id = require_shopping_list_id(str(row["id"]))
            household_id = require_canonical_uuid4(str(row["household_id"]))
            created_by_member_id = require_canonical_uuid4(str(row["created_by_member_id"]))
            status = ShoppingListStatus(str(row["status"]))
            week_start = require_monday_week_start(str(row["week_start"]))
        except (TypeError, ValueError) as exc:
            raise ShoppingStateError("invalid shopping list row") from exc
        source_menu_id = None if row["source_menu_id"] is None else require_canonical_uuid4(str(row["source_menu_id"]))
        source_menu_revision = None if row["source_menu_revision"] is None else int(row["source_menu_revision"])
        version = int(row["version"])
        if version < 1:
            raise ShoppingStateError("invalid shopping list version")
        return ShoppingList(
            id=shopping_list_id,
            household_id=household_id,
            week_start=week_start,
            source_menu_id=source_menu_id,
            source_menu_revision=source_menu_revision,
            status=status,
            created_by_member_id=created_by_member_id,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            completed_at=None if row["completed_at"] is None else str(row["completed_at"]),
            archived_at=None if row["archived_at"] is None else str(row["archived_at"]),
            version=version,
        )

    def _row_to_item(self, row: sqlite3.Row) -> ShoppingItem:
        try:
            item_id = require_shopping_item_id(str(row["id"]))
            shopping_list_id = require_shopping_list_id(str(row["shopping_list_id"]))
            household_id = require_canonical_uuid4(str(row["household_id"]))
            normalized_name = str(row["normalized_name"])
            display_name = str(row["display_name"])
            quantity_value = None if row["quantity_value"] is None else normalize_quantity_value(row["quantity_value"])
            quantity_unit_normalized = normalize_shopping_unit(str(row["quantity_unit_normalized"]))
            quantity_unit_display = str(row["quantity_unit_display"])
            category = None if row["category"] is None else str(row["category"])
            position = int(row["position"])
            checked_state = bool(int(row["checked_state"]))
            origin = ShoppingItemOrigin(str(row["origin"]))
            override_state = ShoppingItemOverrideState(str(row["override_state"]))
            source_menu_entry_id = None if row["source_menu_entry_id"] is None else require_canonical_uuid4(str(row["source_menu_entry_id"]))
            normalization_version = int(row["normalization_version"])
            dedup_fingerprint = str(row["dedup_fingerprint"])
            version = int(row["version"])
        except (TypeError, ValueError) as exc:
            raise ShoppingStateError("invalid shopping item row") from exc
        if not quantity_contract_is_valid(quantity_value, quantity_unit_normalized):
            raise ShoppingStateError("invalid shopping item quantity contract")
        if normalization_version < 1 or version < 1 or position < 1:
            raise ShoppingStateError("invalid shopping item version")
        return ShoppingItem(
            id=item_id,
            shopping_list_id=shopping_list_id,
            household_id=household_id,
            normalized_name=normalized_name,
            display_name=display_name,
            quantity_value=quantity_value,
            quantity_unit_normalized=quantity_unit_normalized,
            quantity_unit_display=quantity_unit_display,
            category=category,
            position=position,
            checked_state=checked_state,
            origin=origin,
            override_state=override_state,
            source_menu_entry_id=source_menu_entry_id,
            normalization_version=normalization_version,
            dedup_fingerprint=dedup_fingerprint,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            version=version,
        )


def _is_valid_uuid(value: object) -> bool:
    try:
        require_canonical_uuid4(str(value))
    except (TypeError, ValueError):
        return False
    return True
