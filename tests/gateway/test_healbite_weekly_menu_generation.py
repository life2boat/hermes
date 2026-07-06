from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_household_schema import HOUSEHOLD_MEMBERS_TABLE, new_household_member_id
from gateway.healbite_households import HealBiteHouseholdStore, HouseholdContext, HouseholdMemberStatus, HouseholdRole, HouseholdStatus
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.healbite_weekly_menu_generation import (
    AuxiliaryWeeklyMenuGenerator,
    CanonicalWeeklyMenuMemberSnapshotProvider,
    HealBiteWeeklyMenuGenerationService,
    WeeklyMenuGenerationStatus,
    WeeklyMenuGeneratedEntry,
    WeeklyMenuGenerationResponse,
    WeeklyMenuGeneratorUnavailableError,
    WeeklyMenuGeneratorValidationError,
    _parse_generation_response,
)
from gateway.healbite_weekly_menu_generation_types import (
    WeeklyMenuGenerationRequest,
    WeeklyMenuMemberGenerationSnapshot,
)
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore, WeeklyMenuEntryInput


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_users_table(db_path: Path, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS users (
                {identity_column} INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _insert_user(db_path: Path, user_id: int, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO users ({identity_column}, username) VALUES (?, ?)",
            (int(user_id), f"user-{user_id}"),
        )


def _seed_generation_runtime(db_path: Path, actor_user_id: int = 101) -> tuple[HealBiteWeeklyMenuStore, HouseholdContext]:
    _create_users_table(db_path)
    _insert_user(db_path, actor_user_id)
    household_store = HealBiteHouseholdStore(db_path=db_path)
    household_store.get_or_create_personal_household(actor_user_id)
    context = household_store.resolve_actor_context(actor_user_id)
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()
    profile_store = HealBiteUserProfileStore(db_path=db_path)
    profile_store.upsert_user_profile(
        user_id=actor_user_id,
        username=f"user-{actor_user_id}",
        daily_kcal_target=1800,
        daily_protein_target=120,
        daily_fat_target=60,
        daily_carbs_target=190,
    )
    return weekly_store, context


def _add_active_member(
    db_path: Path,
    *,
    household_id: str,
    linked_user_id: int,
    role: HouseholdRole,
) -> HouseholdContext:
    _insert_user(db_path, linked_user_id)
    member_id = new_household_member_id()
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, display_name, member_type, role, status, age_band, created_at, updated_at, version)
            VALUES (?, ?, ?, ?, 'linked_adult', ?, 'active', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)
            """,
            (member_id, household_id, linked_user_id, f"user-{linked_user_id}", role.value),
        )
    return HouseholdContext(
        actor_user_id=linked_user_id,
        household_id=household_id,
        household_member_id=member_id,
        role=role,
        member_status=HouseholdMemberStatus.ACTIVE,
        household_status=HouseholdStatus.ACTIVE,
    )


class _StaticGenerator:
    def __init__(self, response: WeeklyMenuGenerationResponse) -> None:
        self.response = response
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return self.response


class _BlockingGenerator:
    def __init__(self, response: WeeklyMenuGenerationResponse) -> None:
        self.response = response
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def generate(self, request):
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=5.0)
        return self.response


class _UnavailableGenerator:
    def generate(self, request):
        raise WeeklyMenuGeneratorUnavailableError("synthetic unavailable")


class _InvalidGenerator:
    def generate(self, request):
        raise WeeklyMenuGeneratorValidationError("synthetic invalid")


def _sample_response(title: str = "Меню") -> WeeklyMenuGenerationResponse:
    return WeeklyMenuGenerationResponse(
        entries=(
            WeeklyMenuGeneratedEntry(
                local_date="2026-07-06",
                meal_slot="breakfast",
                position=1,
                title=title,
                servings="2",
            ),
            WeeklyMenuGeneratedEntry(
                local_date="2026-07-07",
                meal_slot="dinner",
                position=1,
                title=f"{title} 2",
                description="Описание",
            ),
        )
    )


def _service(db_path: Path, *, actor_ids: frozenset[int], generator) -> HealBiteWeeklyMenuGenerationService:
    return HealBiteWeeklyMenuGenerationService(
        generator=generator,
        member_snapshot_provider=CanonicalWeeklyMenuMemberSnapshotProvider(db_path=db_path),
        config=FeatureGateConfig(enabled=True, allowlist=actor_ids, configuration_valid=True),
        db_path=db_path,
    )


def _table_count(db_path: Path, table: str) -> int:
    with _connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_valid_generation_creates_draft_only(tmp_path):
    db_path = tmp_path / "generation.db"
    weekly_store, context = _seed_generation_runtime(db_path)
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=_StaticGenerator(_sample_response("Черновик")))

    result = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-1")

    assert result.success is True
    assert result.revision_view is not None
    assert result.revision_view.revision.status.value == "draft"
    revisions = weekly_store.list_weekly_menu_revisions(context, result.revision_view.series.id)
    assert len(revisions) == 1
    assert sum(1 for revision in revisions if revision.status.value == "published") == 0


def test_valid_generation_replaces_existing_draft(tmp_path):
    db_path = tmp_path / "generation.db"
    weekly_store, context = _seed_generation_runtime(db_path)
    series = weekly_store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = weekly_store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=_StaticGenerator(_sample_response("Замена")))

    result = service.generate_draft_for_week(
        context.actor_user_id,
        "2026-07-06",
        expected_series_version=draft.series.version,
        idempotency_key="gen-1",
    )

    assert result.success is True
    assert result.revision_view is not None
    assert result.revision_view.revision.id == draft.revision.id
    assert [entry.title for entry in result.revision_view.entries] == ["Замена", "Замена 2"]


def test_generation_leaves_existing_published_revision_unchanged(tmp_path):
    db_path = tmp_path / "generation.db"
    weekly_store, context = _seed_generation_runtime(db_path)
    series = weekly_store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = weekly_store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")
    ready = weekly_store.replace_draft_entries(
        context,
        draft.revision.id,
        [
            WeeklyMenuEntryInput(local_date="2026-07-06", meal_slot="breakfast", position=1, title="Опубликованное")
        ],
        expected_revision_version=draft.revision.version,
        idempotency_key="replace-1",
    )
    published = weekly_store.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key="publish-1",
    )
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=_StaticGenerator(_sample_response("Новый черновик")))

    result = service.generate_draft_for_week(
        context.actor_user_id,
        "2026-07-06",
        expected_series_version=published.series.version,
        idempotency_key="gen-1",
    )

    assert result.success is True
    revisions = weekly_store.list_weekly_menu_revisions(context, published.series.id)
    assert sum(1 for revision in revisions if revision.status.value == "published") == 1
    assert any(revision.status.value == "draft" for revision in revisions)
    still_published = weekly_store.get_weekly_menu_revision(context, published.revision.id)
    assert [entry.title for entry in still_published.entries] == ["Опубликованное"]


def test_provider_unavailable_does_not_write_draft(tmp_path):
    db_path = tmp_path / "generation.db"
    _, context = _seed_generation_runtime(db_path)
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=_UnavailableGenerator())

    result = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-1")

    assert result.status is WeeklyMenuGenerationStatus.GENERATOR_UNAVAILABLE
    assert _table_count(db_path, "household_weekly_menus") == 0
    assert _table_count(db_path, "household_weekly_menu_entries") == 0


def test_generation_validation_failure_does_not_write_draft(tmp_path):
    db_path = tmp_path / "generation.db"
    _, context = _seed_generation_runtime(db_path)
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=_InvalidGenerator())

    result = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-1")

    assert result.status is WeeklyMenuGenerationStatus.GENERATOR_VALIDATION_FAILED
    assert _table_count(db_path, "household_weekly_menus") == 0
    assert _table_count(db_path, "household_weekly_menu_entries") == 0


def test_same_key_replay_skips_provider_call(tmp_path):
    db_path = tmp_path / "generation.db"
    _, context = _seed_generation_runtime(db_path)
    generator = _StaticGenerator(_sample_response("Повтор"))
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=generator)

    first = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-1")
    second = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-1")

    assert first.success is True
    assert second.success is True
    assert first.revision_view is not None
    assert second.revision_view is not None
    assert first.revision_view.revision.id == second.revision_view.revision.id
    assert generator.calls == 1


def test_same_key_different_payload_is_typed_conflict(tmp_path):
    db_path = tmp_path / "generation.db"
    _, context = _seed_generation_runtime(db_path)
    generator = _StaticGenerator(_sample_response("Конфликт"))
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=generator)

    first = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-1", locale="ru-RU")
    second = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-1", locale="en-US")

    assert first.success is True
    assert second.status is WeeklyMenuGenerationStatus.IDEMPOTENCY_CONFLICT
    assert generator.calls == 1


def test_no_db_transaction_is_held_during_provider_call(tmp_path):
    db_path = tmp_path / "generation.db"
    _, context = _seed_generation_runtime(db_path)
    generator = _BlockingGenerator(_sample_response("Без блокировки"))
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=generator)
    outcome = {}

    def _run() -> None:
        outcome["result"] = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-1")

    thread = threading.Thread(target=_run)
    thread.start()
    assert generator.started.wait(timeout=5.0) is True
    second = sqlite3.connect(db_path, timeout=0.5, check_same_thread=False)
    try:
        second.execute("BEGIN EXCLUSIVE")
        second.rollback()
    finally:
        second.close()
    generator.release.set()
    thread.join(timeout=5.0)

    result = outcome["result"]
    assert result.success is True


def test_stale_state_change_during_provider_call_returns_conflict_without_partial_write(tmp_path):
    db_path = tmp_path / "generation.db"
    weekly_store, context = _seed_generation_runtime(db_path)
    generator = _BlockingGenerator(_sample_response("Состязание"))
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=generator)
    outcome = {}

    def _run() -> None:
        outcome["result"] = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-1")

    thread = threading.Thread(target=_run)
    thread.start()
    assert generator.started.wait(timeout=5.0) is True
    external_series = weekly_store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    external_draft = weekly_store.create_draft_revision(
        context,
        external_series.id,
        expected_series_version=external_series.version,
        idempotency_key="draft-external",
    )
    generator.release.set()
    thread.join(timeout=5.0)

    result = outcome["result"]
    assert result.status is WeeklyMenuGenerationStatus.VERSION_CONFLICT
    assert _table_count(db_path, "household_weekly_menus") == 1
    latest = weekly_store.get_weekly_menu_revision(context, external_draft.revision.id)
    assert latest.revision.id == external_draft.revision.id
    assert len(latest.entries) == 0


def test_adult_member_is_forbidden_for_generation(tmp_path):
    db_path = tmp_path / "generation.db"
    _, owner_context = _seed_generation_runtime(db_path)
    admin_context = _add_active_member(
        db_path,
        household_id=owner_context.household_id,
        linked_user_id=202,
        role=HouseholdRole.ADULT_ADMIN,
    )
    service = _service(db_path, actor_ids=frozenset({101, 202}), generator=_StaticGenerator(_sample_response("Нет")))

    result = service.generate_draft_for_week(admin_context.actor_user_id, "2026-07-06", idempotency_key="gen-1")

    assert result.status is WeeklyMenuGenerationStatus.FORBIDDEN


def test_generation_response_parser_rejects_unknown_fields_and_duplicates():
    base_request = _sample_request()
    try:
        _parse_generation_response(
            {"entries": [{"local_date": "2026-07-06", "meal_slot": "breakfast", "position": 1, "title": "A", "extra": "x"}]},
            request=base_request,
        )
        assert False, "expected validation error"
    except WeeklyMenuGeneratorValidationError:
        pass
    try:
        _parse_generation_response(
            {
                "entries": [
                    {"local_date": "2026-07-06", "meal_slot": "breakfast", "position": 1, "title": "A"},
                    {"local_date": "2026-07-06", "meal_slot": "breakfast", "position": 1, "title": "B"},
                ]
            },
            request=base_request,
        )
        assert False, "expected validation error"
    except WeeklyMenuGeneratorValidationError:
        pass


def _sample_request():
    return WeeklyMenuGenerationRequest(
        week_start="2026-07-06",
        dates=("2026-07-06", "2026-07-07"),
        allowed_meal_slots=("breakfast", "dinner"),
        locale="ru-RU",
        member_count=1,
        members=(WeeklyMenuMemberGenerationSnapshot(age_band="adult"),),
        household_dietary_notes=(),
        max_entries=4,
    )


def test_auxiliary_generator_wraps_provider_failures_and_validates_json():
    class _Response:
        def __init__(self, content: str) -> None:
            self.choices = [type("Choice", (), {"message": type("Message", (), {"content": content})()})()]

    adapter = AuxiliaryWeeklyMenuGenerator(call_llm_fn=lambda **_: _Response("{\"entries\":[]}"))
    try:
        adapter.generate(_sample_request())
        assert False, "expected validation error"
    except WeeklyMenuGeneratorValidationError:
        pass

    failing = AuxiliaryWeeklyMenuGenerator(call_llm_fn=lambda **_: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        failing.generate(_sample_request())
        assert False, "expected provider error"
    except WeeklyMenuGeneratorUnavailableError:
        pass


def _full_week_response(title: str = "?????? ????") -> WeeklyMenuGenerationResponse:
    dates = ("2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11", "2026-07-12")
    slots = ("breakfast", "lunch", "dinner")
    return WeeklyMenuGenerationResponse(
        entries=tuple(
            WeeklyMenuGeneratedEntry(
                local_date=local_date,
                meal_slot=meal_slot,
                position=1,
                title=f"{title} {local_date} {meal_slot}",
                description="????????",
            )
            for local_date in dates
            for meal_slot in slots
        )
    )


def test_valid_full_week_generation_creates_one_hidden_draft_with_21_entries(tmp_path):
    db_path = tmp_path / "generation.db"
    weekly_store, context = _seed_generation_runtime(db_path)
    generator = _StaticGenerator(_full_week_response())
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=generator)

    result = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-full")

    assert result.success is True
    assert generator.calls == 1
    assert result.revision_view is not None
    assert result.revision_view.revision.status.value == "draft"
    assert len(result.revision_view.entries) == 21
    revisions = weekly_store.list_weekly_menu_revisions(context, result.revision_view.series.id)
    assert sum(1 for revision in revisions if revision.status.value == "published") == 0
    assert _table_count(db_path, "household_weekly_menu_entries") == 21


def test_auxiliary_weekly_generator_uses_strict_single_request_policy(caplog):
    from types import SimpleNamespace

    from agent.auxiliary_client import WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY

    calls = []

    def _fake_call_llm(**kwargs):
        calls.append(kwargs)
        assert kwargs["task"] == "weekly_menu_generation"
        assert kwargs["call_policy"] is WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY
        telemetry = kwargs["request_telemetry"]
        telemetry.external_request_budget = kwargs["call_policy"].max_external_requests
        telemetry.external_request_attempts += 1
        payload = {
            "entries": [
                {"local_date": "2026-07-06", "meal_slot": "breakfast", "position": 1, "title": "????"}
            ]
        }
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])

    request = WeeklyMenuGenerationRequest(
        week_start="2026-07-06",
        dates=("2026-07-06",),
        allowed_meal_slots=("breakfast",),
        locale="ru-RU",
        member_count=1,
        members=(WeeklyMenuMemberGenerationSnapshot(age_band=None),),
        household_dietary_notes=(),
        max_entries=1,
    )
    generator = AuxiliaryWeeklyMenuGenerator(call_llm_fn=_fake_call_llm)

    with caplog.at_level("INFO", logger="gateway.healbite_weekly_menu_generation"):
        response = generator.generate(request)

    assert len(calls) == 1
    assert len(response.entries) == 1
    assert "weekly_menu_provider_call_complete" in caplog.text
    assert "external_request_attempts=1" in caplog.text
    assert "external_request_budget=1" in caplog.text
    assert "retry_performed=False" in caplog.text
    assert "fallback_performed=False" in caplog.text
    assert "101010101" not in caplog.text
    assert "synthetic-api-key" not in caplog.text
    assert "gen-secret-key" not in caplog.text
    assert "????" not in caplog.text


class _DBFailureGenerator:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request):
        self.calls += 1
        return _full_week_response("DB failure")


def test_successful_provider_then_db_conflict_does_not_call_provider_again(tmp_path):
    db_path = tmp_path / "generation.db"
    weekly_store, context = _seed_generation_runtime(db_path)
    series = weekly_store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    generator = _DBFailureGenerator()
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=generator)

    result = service.generate_draft_for_week(
        context.actor_user_id,
        "2026-07-06",
        expected_series_version=series.version + 100,
        idempotency_key="gen-conflict",
    )

    assert result.status is WeeklyMenuGenerationStatus.VERSION_CONFLICT
    assert generator.calls == 0
    assert _table_count(db_path, "household_weekly_menu_entries") == 0


def test_generator_validation_failure_after_one_provider_call_leaves_zero_rows(tmp_path):
    db_path = tmp_path / "generation.db"
    _, context = _seed_generation_runtime(db_path)
    class _CountingInvalidGenerator:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, request):
            self.calls += 1
            raise WeeklyMenuGeneratorValidationError("synthetic invalid")

    generator = _CountingInvalidGenerator()
    service = _service(db_path, actor_ids=frozenset({context.actor_user_id}), generator=generator)

    result = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-invalid")

    assert result.status is WeeklyMenuGenerationStatus.GENERATOR_VALIDATION_FAILED
    assert generator.calls == 1
    assert _table_count(db_path, "household_weekly_menus") == 0
    assert _table_count(db_path, "household_weekly_menu_entries") == 0


class _FailingWeeklyStoreResource:
    cleanup_error = None

    def __enter__(self):
        raise sqlite3.OperationalError("synthetic storage failure")

    def __exit__(self, exc_type, exc, tb):
        return False


class _SecondWeeklyStoreFactoryFails:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def __call__(self):
        from gateway.healbite_runtime_resources import borrowed_runtime_resource

        self.calls += 1
        if self.calls == 1:
            return borrowed_runtime_resource(HealBiteWeeklyMenuStore(db_path=self.db_path))
        return _FailingWeeklyStoreResource()


def test_provider_success_then_db_write_failure_does_not_regenerate_or_persist(tmp_path):
    db_path = tmp_path / "generation.db"
    _, context = _seed_generation_runtime(db_path)
    generator = _StaticGenerator(_full_week_response("DB fail"))
    weekly_factory = _SecondWeeklyStoreFactoryFails(db_path)
    service = HealBiteWeeklyMenuGenerationService(
        generator=generator,
        member_snapshot_provider=CanonicalWeeklyMenuMemberSnapshotProvider(db_path=db_path),
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({context.actor_user_id}), configuration_valid=True),
        db_path=db_path,
        weekly_menu_store_factory=weekly_factory,
    )

    result = service.generate_draft_for_week(context.actor_user_id, "2026-07-06", idempotency_key="gen-db-fail")

    assert result.status is WeeklyMenuGenerationStatus.STORAGE_FAILURE
    assert generator.calls == 1
    assert weekly_factory.calls == 2
    assert _table_count(db_path, "household_weekly_menus") == 0
    assert _table_count(db_path, "household_weekly_menu_entries") == 0
