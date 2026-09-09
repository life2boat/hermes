"""Decision provider protocol and fake implementation for tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ai_engineering.supervisor.context_pack import ContextPack
    from ai_engineering.supervisor.decision import AstraDecision


class DecisionProvider(Protocol):
    """Protocol for Astra decision providers."""

    def decide(self, context: "ContextPack") -> "AstraDecision": ...


class FakeDecisionProvider:
    """Deterministic test provider that returns a pre-loaded decision fixture.

    Makes ZERO real provider calls. Used exclusively in tests.
    """

    def __init__(self, fixture_decision: "AstraDecision") -> None:
        self._decision = fixture_decision

    def decide(self, context: "ContextPack") -> "AstraDecision":
        """Return the pre-loaded fixture decision. Never calls any real provider."""
        return self._decision
