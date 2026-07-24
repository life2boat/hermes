from __future__ import annotations

import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Iterator, Sequence

from gateway.healbite_feature_gates import (
    FeatureAvailabilityStatus,
    FeatureGateConfig,
    evaluate_feature_gate,
    load_feature_gate_config,
)
from gateway.healbite_nutrition_diary import resolve_healbite_db_path
from gateway.healbite_shopping import GeneratedShoppingItemInput
from gateway.healbite_shopping_schema import ShoppingUnit


INVENTORY_SNAPSHOTS_TABLE = "healbite_inventory_snapshots"
INVENTORY_ITEMS_TABLE = "healbite_inventory_items"
_MAX_NAME_LENGTH = 200
_MAX_CATEGORY_LENGTH = 80
_MAX_UNCERTAINTY_LENGTH = 200
_MAX_DECIMAL_LENGTH = 32


class InventoryStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class InventorySourceType(str, Enum):
    TEXT = "text"
    PHOTO = "photo"


class InventoryError(RuntimeError):
    pass


class InventoryNotFoundError(InventoryError):
    pass


class InventoryAccessError(InventoryError):
    pass


class InventoryStateError(InventoryError):
    pass


class InventoryValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InventoryOwnerScope:
    user_id: int | None = None
    household_id: str | None = None

    def __post_init__(self) -> None:
        if (self.user_id is None) == (self.household_id is None):
            raise InventoryValidationError("inventory scope must select exactly one owner")
        if self.user_id is not None and (isinstance(self.user_id, bool) or int(self.user_id) <= 0):
            raise InventoryValidationError("invalid inventory user scope")
        if self.household_id is not None and not str(self.household_id).strip():
            raise InventoryValidationError("invalid inventory household scope")


@dataclass(frozen=True, slots=True)
class InventoryItemInput:
    display_name: str
    quantity_value: str | None = None
    unit: str | ShoppingUnit = ShoppingUnit.UNKNOWN
    category: str | None = None
    confidence: Decimal | str | None = None
    uncertainty: str | None = None


@dataclass(frozen=True, slots=True)
class InventoryItem:
    id: str
    snapshot_id: str
    normalized_name: str
    display_name: str
    quantity_value: str | None
    unit: ShoppingUnit
    category: str | None
    confidence: str | None
    uncertainty: str | None
    position: int


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    id: str
    scope: InventoryOwnerScope
    source_type: InventorySourceType
    status: InventoryStatus
    source_revision: int
    created_at: str
    confirmed_at: str | None
    cancelled_at: str | None


@dataclass(frozen=True, slots=True)
class InventorySnapshotView:
    snapshot: InventorySnapshot
    items: tuple[InventoryItem, ...]


@dataclass(frozen=True, slots=True)
class MissingIngredient:
    normalized_name: str
    display_name: str
    quantity_value: str | None
    unit: ShoppingUnit
    source_meal_keys: tuple[str, ...]
    quantity_unknown: bool = False


@dataclass(frozen=True, slots=True)
class MissingIngredientDelta:
    items: tuple[MissingIngredient, ...]

    def to_shopping_inputs(self, source_entry_ids: dict[str, str]) -> tuple[GeneratedShoppingItemInput, ...]:
        converted: list[GeneratedShoppingItemInput] = []
        for item in self.items:
            source_entry_id = None
            for meal_key in item.source_meal_keys:
                source_entry_id = source_entry_ids.get(meal_key)
                if source_entry_id is not None:
                    break
            converted.append(
                GeneratedShoppingItemInput(
                    display_name=item.display_name,
                    quantity_value=item.quantity_value,
                    quantity_unit_normalized=item.unit,
                    quantity_unit_display=item.unit.value,
                    source_menu_entry_id=source_entry_id,
                )
            )
        return tuple(converted)


_UNIT_ALIASES = {
    "g": ShoppingUnit.G,
    "gr": ShoppingUnit.G,
    "kg": ShoppingUnit.KG,
    "ml": ShoppingUnit.ML,
    "l": ShoppingUnit.L,
    "pcs": ShoppingUnit.PIECE,
    "pc": ShoppingUnit.PIECE,
    "piece": ShoppingUnit.PIECE,
    "pieces": ShoppingUnit.PIECE,
    "package": ShoppingUnit.PACKAGE,
    "pack": ShoppingUnit.PACKAGE,
    "\u0433": ShoppingUnit.G,
    "\u0433\u0440": ShoppingUnit.G,
    "\u0433\u0440.": ShoppingUnit.G,
    "\u043a\u0433": ShoppingUnit.KG,
    "\u043c\u043b": ShoppingUnit.ML,
    "\u043b": ShoppingUnit.L,
    "\u0448\u0442": ShoppingUnit.PIECE,
    "\u0448\u0442.": ShoppingUnit.PIECE,
    "\u0443\u043f": ShoppingUnit.PACKAGE,
    "\u0443\u043f.": ShoppingUnit.PACKAGE,
}
_UNIT_TOKENS = "|".join(re.escape(value) for value in sorted(_UNIT_ALIASES, key=len, reverse=True))
_DECIMAL_TOKEN = r"[0-9]+(?:[.,][0-9]+)?"
_PREFIX_QUANTITY_RE = re.compile(rf"^(?P<quantity>{_DECIMAL_TOKEN})\s*(?P<unit>{_UNIT_TOKENS})\s+(?P<name>.+)$", re.IGNORECASE)
_SUFFIX_QUANTITY_RE = re.compile(rf"^(?P<name>.+?)\s+(?P<quantity>{_DECIMAL_TOKEN})\s*(?P<unit>{_UNIT_TOKENS})$", re.IGNORECASE)
_COUNT_PREFIX_RE = re.compile(rf"^(?P<quantity>{_DECIMAL_TOKEN})\s+(?P<name>.+)$", re.IGNORECASE)


_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {INVENTORY_SNAPSHOTS_TABLE} (
    id TEXT PRIMARY KEY,
    owner_user_id INTEGER NULL,
    household_id TEXT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('text', 'photo')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'cancelled')),
    source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
    created_at TEXT NOT NULL,
    confirmed_at TEXT NULL,
    cancelled_at TEXT NULL,
    CHECK ((owner_user_id IS NOT NULL AND household_id IS NULL) OR (owner_user_id IS NULL AND household_id IS NOT NULL)),
    CHECK ((status = 'pending' AND confirmed_at IS NULL AND cancelled_at IS NULL)
        OR (status = 'confirmed' AND confirmed_at IS NOT NULL AND cancelled_at IS NULL)
        OR (status = 'cancelled' AND confirmed_at IS NULL AND cancelled_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_healbite_inventory_snapshots_user
    ON {INVENTORY_SNAPSHOTS_TABLE} (owner_user_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_healbite_inventory_snapshots_household
    ON {INVENTORY_SNAPSHOTS_TABLE} (household_id, status, created_at);
CREATE TABLE IF NOT EXISTS {INVENTORY_ITEMS_TABLE} (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES {INVENTORY_SNAPSHOTS_TABLE}(id) ON DELETE CASCADE,
    normalized_name TEXT NOT NULL,
    display_name TEXT NOT NULL,
    quantity_value TEXT NULL,
    quantity_unit TEXT NOT NULL CHECK (quantity_unit IN ('g', 'kg', 'ml', 'l', 'piece', 'package', 'unitless', 'unknown')),
    category TEXT NULL,
    confidence TEXT NULL,
    uncertainty TEXT NULL,
    position INTEGER NOT NULL CHECK (position >= 1),
    created_at TEXT NOT NULL,
    UNIQUE (snapshot_id, position)
);
CREATE INDEX IF NOT EXISTS idx_healbite_inventory_items_snapshot
    ON {INVENTORY_ITEMS_TABLE} (snapshot_id, position);
"""


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _normalize_name(value: object) -> tuple[str, str]:
    display = " ".join(unicodedata.normalize("NFKC", str(value)).split())
    if not display or len(display) > _MAX_NAME_LENGTH:
        raise InventoryValidationError("invalid inventory item name")
    return display, display.casefold()


def _normalize_decimal(value: object, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    raw = str(value or "").strip().replace(",", ".")
    if not raw and allow_none:
        return None
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise InventoryValidationError("invalid inventory quantity") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise InventoryValidationError("invalid inventory quantity")
    result = format(parsed.normalize(), "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    if not result or len(result) > _MAX_DECIMAL_LENGTH:
        raise InventoryValidationError("invalid inventory quantity")
    return result


def _normalize_unit(value: str | ShoppingUnit | None) -> ShoppingUnit:
    if isinstance(value, ShoppingUnit):
        return value
    token = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    if token in {"", "unknown"}:
        return ShoppingUnit.UNKNOWN
    if token == "unitless":
        return ShoppingUnit.UNITLESS
    try:
        return ShoppingUnit(token)
    except ValueError:
        normalized = _UNIT_ALIASES.get(token)
        if normalized is None:
            raise InventoryValidationError("invalid inventory unit") from None
        return normalized


def _normalize_optional_text(value: object, *, maximum: int, label: str) -> str | None:
    if value is None:
        return None
    text = " ".join(unicodedata.normalize("NFKC", str(value)).split())
    if not text:
        return None
    if len(text) > maximum:
        raise InventoryValidationError(f"invalid inventory {label}")
    return text


def _normalize_confidence(value: Decimal | str | None) -> str | None:
    if value is None:
        return None
    raw = _normalize_decimal(value)
    assert raw is not None
    parsed = Decimal(raw)
    if parsed > Decimal("1"):
        raise InventoryValidationError("invalid inventory confidence")
    return raw


def _normalize_item_input(item: InventoryItemInput) -> dict[str, object]:
    display_name, normalized_name = _normalize_name(item.display_name)
    quantity_value = _normalize_decimal(item.quantity_value, allow_none=True)
    unit = _normalize_unit(item.unit)
    if quantity_value is not None and unit is ShoppingUnit.UNKNOWN:
        raise InventoryValidationError("known inventory quantity requires a compatible unit")
    return {
        "display_name": display_name,
        "normalized_name": normalized_name,
        "quantity_value": quantity_value,
        "unit": unit,
        "category": _normalize_optional_text(item.category, maximum=_MAX_CATEGORY_LENGTH, label="category"),
        "confidence": _normalize_confidence(item.confidence),
        "uncertainty": _normalize_optional_text(item.uncertainty, maximum=_MAX_UNCERTAINTY_LENGTH, label="uncertainty"),
    }


def parse_inventory_text(text: str) -> tuple[InventoryItemInput, ...]:
    chunks = [" ".join(chunk.split()) for chunk in re.split(r"[,\n]+", str(text))]
    parsed: list[InventoryItemInput] = []
    for chunk in chunks:
        if not chunk:
            continue
        match = _PREFIX_QUANTITY_RE.match(chunk) or _SUFFIX_QUANTITY_RE.match(chunk)
        if match is not None:
            parsed.append(
                InventoryItemInput(
                    display_name=match.group("name"),
                    quantity_value=match.group("quantity"),
                    unit=match.group("unit"),
                )
            )
            continue
        match = _COUNT_PREFIX_RE.match(chunk)
        if match is not None:
            parsed.append(
                InventoryItemInput(
                    display_name=match.group("name"),
                    quantity_value=match.group("quantity"),
                    unit=ShoppingUnit.PIECE,
                )
            )
            continue
        parsed.append(InventoryItemInput(display_name=chunk))
    if not parsed:
        raise InventoryValidationError("inventory text contains no items")
    return tuple(parsed)


class HealBiteInventoryStore:
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        connection: sqlite3.Connection | None = None,
        ensure_schema_on_init: bool = True,
    ) -> None:
        if connection is not None and db_path is not None:
            raise ValueError("choose db_path or connection")
        self._connection = connection
        self._owns_connection = connection is None
        if connection is not None and connection.row_factory is None:
            connection.row_factory = sqlite3.Row
        self._db_path = resolve_healbite_db_path(db_path) if connection is None else None
        if ensure_schema_on_init:
            self.initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        assert self._db_path is not None
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _connection_scope(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            if self._owns_connection:
                conn.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection_scope() as conn:
            if self._owns_connection:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                if self._owns_connection:
                    conn.rollback()
                raise
            else:
                if self._owns_connection:
                    conn.commit()

    def initialize_schema(self) -> None:
        with self._connection_scope() as conn:
            conn.executescript(_SCHEMA_SQL)
            if self._owns_connection:
                conn.commit()

    def create_snapshot(
        self,
        scope: InventoryOwnerScope,
        source_type: InventorySourceType | str,
        items: Sequence[InventoryItemInput],
    ) -> InventorySnapshotView:
        try:
            source = source_type if isinstance(source_type, InventorySourceType) else InventorySourceType(str(source_type))
        except ValueError as exc:
            raise InventoryValidationError("invalid inventory source type") from exc
        normalized_items = [_normalize_item_input(item) for item in items]
        if not normalized_items:
            raise InventoryValidationError("inventory snapshot must contain items")
        snapshot_id = str(uuid.uuid4())
        now = _timestamp()
        with self._write_transaction() as conn:
            conn.execute(
                f"""
                INSERT INTO {INVENTORY_SNAPSHOTS_TABLE}
                    (id, owner_user_id, household_id, source_type, status, source_revision, created_at, confirmed_at, cancelled_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, NULL, NULL)
                """,
                (snapshot_id, scope.user_id, scope.household_id, source.value, InventoryStatus.PENDING.value, now),
            )
            for position, item in enumerate(normalized_items, start=1):
                conn.execute(
                    f"""
                    INSERT INTO {INVENTORY_ITEMS_TABLE}
                        (id, snapshot_id, normalized_name, display_name, quantity_value, quantity_unit, category, confidence, uncertainty, position, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        snapshot_id,
                        item["normalized_name"],
                        item["display_name"],
                        item["quantity_value"],
                        item["unit"].value,
                        item["category"],
                        item["confidence"],
                        item["uncertainty"],
                        position,
                        now,
                    ),
                )
            return self._load_snapshot(conn, snapshot_id, scope)

    def create_text_snapshot(self, scope: InventoryOwnerScope, text: str) -> InventorySnapshotView:
        return self.create_snapshot(scope, InventorySourceType.TEXT, parse_inventory_text(text))

    def create_photo_candidate(
        self,
        scope: InventoryOwnerScope,
        candidates: Sequence[InventoryItemInput],
    ) -> InventorySnapshotView:
        return self.create_snapshot(scope, InventorySourceType.PHOTO, candidates)

    def get_snapshot(self, scope: InventoryOwnerScope, snapshot_id: str) -> InventorySnapshotView:
        with self._connection_scope() as conn:
            return self._load_snapshot(conn, snapshot_id, scope)

    def get_confirmed_snapshot(self, scope: InventoryOwnerScope, snapshot_id: str) -> InventorySnapshotView:
        view = self.get_snapshot(scope, snapshot_id)
        if view.snapshot.status is not InventoryStatus.CONFIRMED:
            raise InventoryStateError("inventory snapshot is not confirmed")
        return view

    def get_latest_confirmed_snapshot(
        self,
        scope: InventoryOwnerScope,
    ) -> InventorySnapshotView | None:
        owner_column = "owner_user_id" if scope.user_id is not None else "household_id"
        owner_value = scope.user_id if scope.user_id is not None else scope.household_id
        with self._connection_scope() as conn:
            row = conn.execute(
                f"""
                SELECT id
                FROM {INVENTORY_SNAPSHOTS_TABLE}
                WHERE {owner_column} = ? AND status = ?
                ORDER BY confirmed_at DESC, id DESC
                LIMIT 1
                """,
                (owner_value, InventoryStatus.CONFIRMED.value),
            ).fetchone()
            if row is None:
                return None
            return self._load_snapshot(conn, str(row["id"]), scope)

    def replace_pending_items(
        self,
        scope: InventoryOwnerScope,
        snapshot_id: str,
        items: Sequence[InventoryItemInput],
        *,
        expected_source_revision: int | None = None,
    ) -> InventorySnapshotView:
        normalized_items = [_normalize_item_input(item) for item in items]
        if not normalized_items:
            raise InventoryValidationError("inventory snapshot must contain items")
        with self._write_transaction() as conn:
            view = self._load_snapshot(conn, snapshot_id, scope)
            if view.snapshot.status is not InventoryStatus.PENDING:
                raise InventoryStateError("only pending inventory snapshots may be edited")
            if (
                expected_source_revision is not None
                and view.snapshot.source_revision != int(expected_source_revision)
            ):
                raise InventoryStateError("inventory snapshot revision changed")
            conn.execute(f"DELETE FROM {INVENTORY_ITEMS_TABLE} WHERE snapshot_id = ?", (view.snapshot.id,))
            now = _timestamp()
            for position, item in enumerate(normalized_items, start=1):
                conn.execute(
                    f"""
                    INSERT INTO {INVENTORY_ITEMS_TABLE}
                        (id, snapshot_id, normalized_name, display_name, quantity_value, quantity_unit, category, confidence, uncertainty, position, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (str(uuid.uuid4()), view.snapshot.id, item["normalized_name"], item["display_name"], item["quantity_value"], item["unit"].value, item["category"], item["confidence"], item["uncertainty"], position, now),
                )
            conn.execute(
                f"UPDATE {INVENTORY_SNAPSHOTS_TABLE} SET source_revision = source_revision + 1 WHERE id = ?",
                (view.snapshot.id,),
            )
            return self._load_snapshot(conn, view.snapshot.id, scope)

    def confirm_snapshot(
        self,
        scope: InventoryOwnerScope,
        snapshot_id: str,
        *,
        expected_source_revision: int | None = None,
    ) -> InventorySnapshotView:
        with self._write_transaction() as conn:
            view = self._load_snapshot(conn, snapshot_id, scope)
            if (
                expected_source_revision is not None
                and view.snapshot.source_revision != int(expected_source_revision)
            ):
                raise InventoryStateError("inventory snapshot revision changed")
            if view.snapshot.status is InventoryStatus.CONFIRMED:
                return view
            if view.snapshot.status is not InventoryStatus.PENDING:
                raise InventoryStateError("inventory snapshot may not be confirmed")
            conn.execute(
                f"UPDATE {INVENTORY_SNAPSHOTS_TABLE} SET status = ?, confirmed_at = ? WHERE id = ? AND status = ?",
                (InventoryStatus.CONFIRMED.value, _timestamp(), view.snapshot.id, InventoryStatus.PENDING.value),
            )
            return self._load_snapshot(conn, view.snapshot.id, scope)

    def cancel_snapshot(
        self,
        scope: InventoryOwnerScope,
        snapshot_id: str,
        *,
        expected_source_revision: int | None = None,
    ) -> InventorySnapshotView:
        with self._write_transaction() as conn:
            view = self._load_snapshot(conn, snapshot_id, scope)
            if (
                expected_source_revision is not None
                and view.snapshot.source_revision != int(expected_source_revision)
            ):
                raise InventoryStateError("inventory snapshot revision changed")
            if view.snapshot.status is InventoryStatus.CANCELLED:
                return view
            if view.snapshot.status is not InventoryStatus.PENDING:
                raise InventoryStateError("inventory snapshot may not be cancelled")
            conn.execute(
                f"UPDATE {INVENTORY_SNAPSHOTS_TABLE} SET status = ?, cancelled_at = ? WHERE id = ? AND status = ?",
                (InventoryStatus.CANCELLED.value, _timestamp(), view.snapshot.id, InventoryStatus.PENDING.value),
            )
            return self._load_snapshot(conn, view.snapshot.id, scope)

    def _load_snapshot(self, conn: sqlite3.Connection, snapshot_id: str, scope: InventoryOwnerScope) -> InventorySnapshotView:
        row = conn.execute(f"SELECT * FROM {INVENTORY_SNAPSHOTS_TABLE} WHERE id = ? LIMIT 1", (str(snapshot_id),)).fetchone()
        if row is None:
            raise InventoryNotFoundError("inventory snapshot not found")
        if (scope.user_id is not None and row["owner_user_id"] != int(scope.user_id)) or (scope.household_id is not None and row["household_id"] != str(scope.household_id)):
            raise InventoryAccessError("inventory snapshot out of scope")
        snapshot = InventorySnapshot(
            id=str(row["id"]),
            scope=InventoryOwnerScope(user_id=row["owner_user_id"], household_id=row["household_id"]),
            source_type=InventorySourceType(str(row["source_type"])),
            status=InventoryStatus(str(row["status"])),
            source_revision=int(row["source_revision"]),
            created_at=str(row["created_at"]),
            confirmed_at=None if row["confirmed_at"] is None else str(row["confirmed_at"]),
            cancelled_at=None if row["cancelled_at"] is None else str(row["cancelled_at"]),
        )
        item_rows = conn.execute(f"SELECT * FROM {INVENTORY_ITEMS_TABLE} WHERE snapshot_id = ? ORDER BY position ASC", (snapshot.id,)).fetchall()
        return InventorySnapshotView(
            snapshot=snapshot,
            items=tuple(
                InventoryItem(
                    id=str(item["id"]),
                    snapshot_id=snapshot.id,
                    normalized_name=str(item["normalized_name"]),
                    display_name=str(item["display_name"]),
                    quantity_value=None if item["quantity_value"] is None else str(item["quantity_value"]),
                    unit=ShoppingUnit(str(item["quantity_unit"])),
                    category=None if item["category"] is None else str(item["category"]),
                    confidence=None if item["confidence"] is None else str(item["confidence"]),
                    uncertainty=None if item["uncertainty"] is None else str(item["uncertainty"]),
                    position=int(item["position"]),
                )
                for item in item_rows
            ),
        )


class HealBiteInventoryInputService:
    def __init__(
        self,
        store: HealBiteInventoryStore,
        *,
        text_config: FeatureGateConfig | None = None,
        photo_config: FeatureGateConfig | None = None,
    ) -> None:
        self._store = store
        self._text_config = text_config if text_config is not None else load_feature_gate_config("HEALBITE_INVENTORY_TEXT")
        self._photo_config = photo_config if photo_config is not None else load_feature_gate_config("HEALBITE_INVENTORY_PHOTO")

    @staticmethod
    def _require_gate(config: FeatureGateConfig, actor_user_id: object) -> None:
        decision = evaluate_feature_gate(config, actor_user_id)
        if not decision.ready:
            raise InventoryStateError(f"inventory input unavailable: {decision.status.value}")

    def create_text_snapshot(self, actor_user_id: object, scope: InventoryOwnerScope, text: str) -> InventorySnapshotView:
        self._require_gate(self._text_config, actor_user_id)
        return self._store.create_text_snapshot(scope, text)

    def create_photo_candidate(self, actor_user_id: object, scope: InventoryOwnerScope, candidates: Sequence[InventoryItemInput]) -> InventorySnapshotView:
        self._require_gate(self._photo_config, actor_user_id)
        return self._store.create_photo_candidate(scope, candidates)


def calculate_missing_ingredients(
    requirements: Sequence[tuple[str, InventoryItemInput]],
    inventory: InventorySnapshotView,
) -> MissingIngredientDelta:
    if inventory.snapshot.status is not InventoryStatus.CONFIRMED:
        raise InventoryStateError("inventory snapshot is not confirmed")
    available: dict[tuple[str, ShoppingUnit], Decimal] = {}
    for item in inventory.items:
        if item.quantity_value is None or item.unit is ShoppingUnit.UNKNOWN:
            continue
        key = (item.normalized_name, item.unit)
        available[key] = available.get(key, Decimal("0")) + Decimal(item.quantity_value)
    grouped: dict[tuple[str, ShoppingUnit], dict[str, object]] = {}
    for meal_key, source in requirements:
        normalized = _normalize_item_input(source)
        quantity_value = normalized["quantity_value"]
        unit = normalized["unit"]
        if quantity_value is None or unit is ShoppingUnit.UNKNOWN:
            key = (str(normalized["normalized_name"]), unit)
            group = grouped.setdefault(key, {"display_name": normalized["display_name"], "quantity": None, "meal_keys": set(), "unknown": True})
            group["meal_keys"].add(str(meal_key))
            continue
        key = (str(normalized["normalized_name"]), unit)
        group = grouped.setdefault(key, {"display_name": normalized["display_name"], "quantity": Decimal("0"), "meal_keys": set(), "unknown": False})
        group["quantity"] += Decimal(str(quantity_value))
        group["meal_keys"].add(str(meal_key))
    missing: list[MissingIngredient] = []
    for (normalized_name, unit), group in grouped.items():
        requested = group["quantity"]
        if requested is None:
            missing.append(MissingIngredient(normalized_name, str(group["display_name"]), None, unit, tuple(sorted(group["meal_keys"])), True))
            continue
        remainder = requested - available.get((normalized_name, unit), Decimal("0"))
        if remainder <= 0:
            continue
        quantity = format(remainder.normalize(), "f")
        if "." in quantity:
            quantity = quantity.rstrip("0").rstrip(".")
        missing.append(MissingIngredient(normalized_name, str(group["display_name"]), quantity, unit, tuple(sorted(group["meal_keys"]))))
    return MissingIngredientDelta(items=tuple(missing))
