from __future__ import annotations

from gateway.memory.orphan_classifier import classify_historical_points
from gateway.memory.qdrant_adapter import QdrantMemoryAdapter


def _point(user_id, fact_id, revision, *, point_id=None, payload=True):
    return {
        "id": point_id
        or QdrantMemoryAdapter.point_id(sqlite_id=fact_id, user_id=user_id),
        "payload": (
            {
                "user_id": user_id,
                "sqlite_id": fact_id,
                "vector_revision": revision,
            }
            if payload
            else None
        ),
    }


def test_offline_classifier_covers_safe_historical_classes_without_delete_authority():
    facts = [
        {"id": 1, "user_id": 101, "vector_revision": 2},
        {"id": 2, "user_id": 202, "vector_revision": 1},
    ]
    points = [
        _point(101, 1, 2),
        _point(101, 99, 1),
        _point(101, 2, 1),
        _point("bad", 1, 1),
        _point(101, 1, 1),
        _point(101, 1, 2, point_id="not-current-identity"),
    ]
    report = classify_historical_points(canonical_facts=facts, points=points)
    assert [record.classification for record in report.records] == [
        "CANONICAL_MATCH",
        "MISSING_IN_SQLITE",
        "FOREIGN_OWNER",
        "MALFORMED_PAYLOAD",
        "UNKNOWN",
        "UNKNOWN",
    ]
    assert report.deletion_authorized is False
    assert "value" not in repr(report.as_dict())


def test_duplicate_current_identity_is_classified_not_deleted():
    fact = {"id": 1, "user_id": 101, "vector_revision": 1}
    report = classify_historical_points(
        canonical_facts=[fact],
        points=[_point(101, 1, 1), _point(101, 1, 1)],
    )
    assert report.counts["DUPLICATE"] == 2
    assert report.deletion_authorized is False
