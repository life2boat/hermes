from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from gateway.memory.qdrant_adapter import QdrantMemoryAdapter

_CLASSIFICATIONS = (
    "CANONICAL_MATCH",
    "MISSING_IN_SQLITE",
    "FOREIGN_OWNER",
    "MALFORMED_PAYLOAD",
    "DUPLICATE",
    "UNKNOWN",
)


@dataclass(frozen=True, slots=True)
class OrphanClassification:
    ordinal: int
    classification: str


@dataclass(frozen=True, slots=True)
class OrphanClassificationReport:
    total: int
    counts: dict[str, int]
    records: tuple[OrphanClassification, ...]
    deletion_authorized: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_historical_points(
    *,
    canonical_facts: Iterable[Mapping[str, Any]],
    points: Iterable[Mapping[str, Any]],
) -> OrphanClassificationReport:
    """Classify sanitized point metadata offline; never authorize deletion."""
    facts: dict[int, tuple[int, int]] = {}
    for fact in canonical_facts:
        fact_id = fact.get("id")
        user_id = fact.get("user_id")
        revision = fact.get("vector_revision")
        if all(type(value) is int and value > 0 for value in (fact_id, user_id, revision)):
            facts[int(fact_id)] = (int(user_id), int(revision))

    materialized = list(points)
    identities: Counter[tuple[int, int, int]] = Counter()
    for point in materialized:
        payload = point.get("payload")
        if isinstance(payload, Mapping):
            values = (
                payload.get("user_id"),
                payload.get("sqlite_id"),
                payload.get("vector_revision"),
            )
            if all(type(value) is int and value > 0 for value in values):
                expected = QdrantMemoryAdapter.point_id(
                    sqlite_id=int(values[1]), user_id=int(values[0])
                )
                if point.get("id") == expected:
                    identities[(int(values[0]), int(values[1]), int(values[2]))] += 1

    records: list[OrphanClassification] = []
    counts: Counter[str] = Counter()
    for ordinal, point in enumerate(materialized, start=1):
        classification = _classify_one(point, facts=facts, identities=identities)
        records.append(OrphanClassification(ordinal=ordinal, classification=classification))
        counts[classification] += 1
    return OrphanClassificationReport(
        total=len(records),
        counts={name: counts.get(name, 0) for name in _CLASSIFICATIONS},
        records=tuple(records),
    )


def _classify_one(
    point: Mapping[str, Any],
    *,
    facts: Mapping[int, tuple[int, int]],
    identities: Counter[tuple[int, int, int]],
) -> str:
    payload = point.get("payload")
    if not isinstance(payload, Mapping):
        return "MALFORMED_PAYLOAD"
    user_id = payload.get("user_id")
    fact_id = payload.get("sqlite_id")
    revision = payload.get("vector_revision")
    if not all(type(value) is int and value > 0 for value in (user_id, fact_id, revision)):
        return "MALFORMED_PAYLOAD"
    identity = (int(user_id), int(fact_id), int(revision))
    expected_point_id = QdrantMemoryAdapter.point_id(
        sqlite_id=int(fact_id), user_id=int(user_id)
    )
    point_id = point.get("id")
    if not isinstance(point_id, str) or point_id != expected_point_id:
        return "UNKNOWN"
    if identities[identity] > 1:
        return "DUPLICATE"
    canonical = facts.get(int(fact_id))
    if canonical is None:
        return "MISSING_IN_SQLITE"
    canonical_owner, canonical_revision = canonical
    if canonical_owner != int(user_id):
        return "FOREIGN_OWNER"
    if canonical_revision != int(revision):
        return "UNKNOWN"
    return "CANONICAL_MATCH"
