from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


class RuntimeResourceOwnership(str, Enum):
    OWNED = "owned"
    BORROWED = "borrowed"


@dataclass(frozen=True, slots=True)
class RuntimeResourceContract:
    ownership: RuntimeResourceOwnership

    @property
    def owned(self) -> bool:
        return self.ownership is RuntimeResourceOwnership.OWNED


class RuntimeResource(Generic[T]):
    def __init__(
        self,
        resource: T,
        *,
        contract: RuntimeResourceContract,
        cleanup: Callable[[T], None] | None = None,
        rollback_before_close: Callable[[T], None] | None = None,
    ) -> None:
        self._resource = resource
        self._contract = contract
        self._cleanup = cleanup
        self._rollback_before_close = rollback_before_close
        self._entered = False
        self._exited = False
        self._cleanup_error: Exception | None = None

    @property
    def contract(self) -> RuntimeResourceContract:
        return self._contract

    @property
    def cleanup_error(self) -> Exception | None:
        return self._cleanup_error

    def __enter__(self) -> T:
        self._entered = True
        return self._resource

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._exited:
            return False
        self._exited = True
        if not self._contract.owned:
            return False
        try:
            if self._rollback_before_close is not None:
                self._rollback_before_close(self._resource)
            if self._cleanup is not None:
                self._cleanup(self._resource)
        except Exception as error:  # pragma: no cover - asserted through cleanup_error.
            self._cleanup_error = error
        return False


def owned_runtime_resource(
    resource: T,
    *,
    cleanup: Callable[[T], None],
    rollback_before_close: Callable[[T], None] | None = None,
) -> RuntimeResource[T]:
    return RuntimeResource(
        resource,
        contract=RuntimeResourceContract(RuntimeResourceOwnership.OWNED),
        cleanup=cleanup,
        rollback_before_close=rollback_before_close,
    )


def borrowed_runtime_resource(resource: T) -> RuntimeResource[T]:
    return RuntimeResource(
        resource,
        contract=RuntimeResourceContract(RuntimeResourceOwnership.BORROWED),
    )
