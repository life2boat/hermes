from __future__ import annotations

import sqlite3
from pathlib import Path

from gateway.healbite_runtime_resources import (
    RuntimeResourceOwnership,
    borrowed_runtime_resource,
    owned_runtime_resource,
)


class _LifecycleTarget:
    def __init__(self) -> None:
        self.close_calls = 0
        self.rollback_calls = 0

    def close(self) -> None:
        self.close_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1


def test_owned_runtime_resource_rolls_back_before_close() -> None:
    target = _LifecycleTarget()
    resource = owned_runtime_resource(
        target,
        cleanup=lambda value: value.close(),
        rollback_before_close=lambda value: value.rollback(),
    )

    with resource as leased:
        assert leased is target

    assert resource.contract.ownership is RuntimeResourceOwnership.OWNED
    assert target.rollback_calls == 1
    assert target.close_calls == 1
    assert resource.cleanup_error is None


def test_borrowed_runtime_resource_never_rolls_back_or_closes() -> None:
    target = _LifecycleTarget()
    resource = borrowed_runtime_resource(target)

    with resource as leased:
        assert leased is target

    assert resource.contract.ownership is RuntimeResourceOwnership.BORROWED
    assert target.rollback_calls == 0
    assert target.close_calls == 0
    assert resource.cleanup_error is None


def test_owned_runtime_resource_captures_cleanup_failure() -> None:
    target = _LifecycleTarget()

    def _cleanup(_value: _LifecycleTarget) -> None:
        raise RuntimeError("synthetic cleanup failure")

    resource = owned_runtime_resource(target, cleanup=_cleanup)

    with resource:
        pass

    assert isinstance(resource.cleanup_error, RuntimeError)
    assert target.close_calls == 0


def test_owned_runtime_resource_rolls_back_open_transaction_before_close(tmp_path: Path) -> None:
    db_path = tmp_path / "resource.db"
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")

    resource = owned_runtime_resource(
        conn,
        cleanup=lambda value: value.close(),
        rollback_before_close=lambda value: value.rollback() if value.in_transaction else None,
    )

    with resource as leased:
        assert leased.in_transaction is True

    assert resource.cleanup_error is None
    second = sqlite3.connect(db_path, timeout=0.5, check_same_thread=False)
    try:
        second.execute("BEGIN EXCLUSIVE")
        second.rollback()
    finally:
        second.close()
