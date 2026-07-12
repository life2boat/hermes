from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, Sequence

from agent.auxiliary_client import (
    ExternalRequestTelemetry,
    LLMServiceUnavailableError,
    WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
    extract_content_or_reasoning,
    safe_call_llm,
)
from gateway.healbite_feature_gates import (
    FeatureAvailabilityStatus,
    FeatureGateConfig,
    FeatureGateDecision,
    evaluate_feature_gate,
    load_feature_gate_config,
)
from gateway.healbite_household_schema import HouseholdMemberStatus, HouseholdRole, HouseholdStatus
from gateway.healbite_households import (
    HealBiteHouseholdService,
    HealBiteHouseholdStore,
    HouseholdAccessError,
    HouseholdIntegrityError,
    HouseholdNotFoundError,
    HouseholdValidationError,
)
from gateway.healbite_nutrition_diary import resolve_healbite_db_path
from gateway.healbite_runtime_resources import RuntimeResource, borrowed_runtime_resource
from gateway.healbite_user_profile import (
    HealBiteUserProfileStore,
    HealBiteWeeklyMenuProfileSnapshot,
)
from gateway.healbite_weekly_menu_generation_types import (
    WeeklyMenuGeneratedEntry,
    WeeklyMenuGenerationRequest,
    WeeklyMenuGenerationResponse,
    WeeklyMenuMemberGenerationSnapshot,
)
from gateway.healbite_weekly_menu_schema import (
    MEAL_SLOT_ORDER,
    WeeklyMenuEntryOrigin,
    WeeklyMenuSchemaState,
    normalize_week_start,
    require_monday_week_start,
    week_dates,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    HouseholdAuthorizationContext,
    WeeklyMenuConflictError,
    WeeklyMenuEntryInput,
    WeeklyMenuRevisionStatus,
    WeeklyMenuRevisionView,
    WeeklyMenuStateError,
    WeeklyMenuValidationError,
)

HouseholdStoreResourceFactory = Callable[[], RuntimeResource[HealBiteHouseholdStore]]
WeeklyMenuStoreResourceFactory = Callable[[], RuntimeResource[HealBiteWeeklyMenuStore]]
ProfileStoreFactory = Callable[[], HealBiteUserProfileStore]

logger = logging.getLogger(__name__)

_MAX_GENERATION_ENTRIES = 56
_MAX_DIETARY_NOTES = 8


class WeeklyMenuGeneratorUnavailableError(RuntimeError):
    pass


class WeeklyMenuGeneratorValidationError(ValueError):
    pass


class WeeklyMenuGenerationStatus(str, Enum):
    SUCCESS = "success"
    DISABLED = "disabled"
    MISCONFIGURED = "misconfigured"
    INVALID_ACTOR = "invalid_actor"
    NOT_ALLOWLISTED = "not_allowlisted"
    HOUSEHOLD_UNAVAILABLE = "household_unavailable"
    SCHEMA_UNAVAILABLE = "schema_unavailable"
    FORBIDDEN = "forbidden"
    VALIDATION_FAILED = "validation_failed"
    VERSION_CONFLICT = "version_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_STATE = "invalid_state"
    GENERATOR_UNAVAILABLE = "generator_unavailable"
    GENERATOR_VALIDATION_FAILED = "generator_validation_failed"
    STORAGE_FAILURE = "storage_failure"
    CLEANUP_FAILURE = "cleanup_failure"


@dataclass(frozen=True, slots=True)
class WeeklyMenuGenerationResult:
    status: WeeklyMenuGenerationStatus
    revision_view: WeeklyMenuRevisionView | None = None
    feature_status: FeatureAvailabilityStatus | None = None

    @property
    def success(self) -> bool:
        return self.status is WeeklyMenuGenerationStatus.SUCCESS and self.revision_view is not None


class WeeklyMenuGenerator(Protocol):
    def generate(self, request: WeeklyMenuGenerationRequest) -> WeeklyMenuGenerationResponse:
        ...


class WeeklyMenuMemberSnapshotProvider(Protocol):
    def build_request(
        self,
        context: HouseholdAuthorizationContext,
        *,
        week_start: str,
        locale: str,
        max_entries: int,
    ) -> WeeklyMenuGenerationRequest:
        ...


def _gate_failure_result(decision: FeatureGateDecision) -> WeeklyMenuGenerationResult:
    status_map = {
        FeatureAvailabilityStatus.DISABLED: WeeklyMenuGenerationStatus.DISABLED,
        FeatureAvailabilityStatus.MISCONFIGURED: WeeklyMenuGenerationStatus.MISCONFIGURED,
        FeatureAvailabilityStatus.INVALID_ACTOR: WeeklyMenuGenerationStatus.INVALID_ACTOR,
        FeatureAvailabilityStatus.NOT_ALLOWLISTED: WeeklyMenuGenerationStatus.NOT_ALLOWLISTED,
        FeatureAvailabilityStatus.HOUSEHOLD_UNAVAILABLE: WeeklyMenuGenerationStatus.HOUSEHOLD_UNAVAILABLE,
        FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE: WeeklyMenuGenerationStatus.SCHEMA_UNAVAILABLE,
    }
    return WeeklyMenuGenerationResult(
        status=status_map[decision.status],
        feature_status=decision.status,
    )


def _normalize_max_entries(value: int) -> int:
    parsed = int(value)
    if parsed <= 0 or parsed > _MAX_GENERATION_ENTRIES:
        raise WeeklyMenuValidationError("invalid max_entries")
    return parsed


def _normalize_household_note(value: str) -> str | None:
    collapsed = " ".join(str(value or "").split()).strip()
    if not collapsed:
        return None
    return collapsed[:200]


def _dedupe_notes(values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        note = _normalize_household_note(value)
        if note is None or note in seen:
            continue
        normalized.append(note)
        seen.add(note)
        if len(normalized) >= _MAX_DIETARY_NOTES:
            break
    return tuple(normalized)


def _request_payload_fingerprint(request: WeeklyMenuGenerationRequest) -> str:
    payload = {
        "week_start": request.week_start,
        "dates": list(request.dates),
        "allowed_meal_slots": list(request.allowed_meal_slots),
        "locale": request.locale,
        "member_count": request.member_count,
        "members": [
            {
                "age_band": member.age_band,
                "daily_kcal_target": member.daily_kcal_target,
                "daily_protein_g": member.daily_protein_g,
                "daily_fat_g": member.daily_fat_g,
                "daily_carbs_g": member.daily_carbs_g,
                "dietary_notes": list(member.dietary_notes),
            }
            for member in request.members
        ],
        "household_dietary_notes": list(request.household_dietary_notes),
        "max_entries": request.max_entries,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _generation_idempotency_key(idempotency_key: str) -> str:
    normalized = str(idempotency_key).strip()
    if not normalized:
        raise WeeklyMenuValidationError("invalid idempotency key")
    return "generate:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class CanonicalWeeklyMenuMemberSnapshotProvider:
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        household_store_factory: Callable[[], HealBiteHouseholdStore] | None = None,
        profile_store_factory: ProfileStoreFactory | None = None,
    ) -> None:
        self._db_path = resolve_healbite_db_path(db_path)
        self._household_store_factory = household_store_factory or self._default_household_store_factory
        self._profile_store_factory = profile_store_factory or self._default_profile_store_factory

    def _default_household_store_factory(self) -> HealBiteHouseholdStore:
        return HealBiteHouseholdStore(db_path=self._db_path, ensure_schema_on_init=False)

    def _default_profile_store_factory(self) -> HealBiteUserProfileStore:
        return HealBiteUserProfileStore(db_path=self._db_path, ensure_schema_on_init=False)

    def build_request(
        self,
        context: HouseholdAuthorizationContext,
        *,
        week_start: str,
        locale: str,
        max_entries: int,
    ) -> WeeklyMenuGenerationRequest:
        canonical_week_start = require_monday_week_start(normalize_week_start(str(week_start).strip()))
        normalized_max_entries = _normalize_max_entries(max_entries)
        household_store = self._household_store_factory()
        profile_store = self._profile_store_factory()
        household_service = HealBiteHouseholdService(household_store)
        members = [
            member
            for member in household_service.list_members_for_actor(
                context.actor_user_id,
                context.household_id,
            )
            if member.status is HouseholdMemberStatus.ACTIVE
        ]
        member_snapshots: list[WeeklyMenuMemberGenerationSnapshot] = []
        household_notes: list[str] = []
        for member in members:
            profile_snapshot: HealBiteWeeklyMenuProfileSnapshot | None = None
            if member.linked_user_id is not None:
                profile_snapshot = profile_store.get_weekly_menu_profile_snapshot(member.linked_user_id)
            dietary_notes = () if profile_snapshot is None else profile_snapshot.dietary_notes
            household_notes.extend(dietary_notes)
            member_snapshots.append(
                WeeklyMenuMemberGenerationSnapshot(
                    age_band=member.age_band,
                    daily_kcal_target=None if profile_snapshot is None else profile_snapshot.daily_kcal_target,
                    daily_protein_g=None if profile_snapshot is None else profile_snapshot.daily_protein_g,
                    daily_fat_g=None if profile_snapshot is None else profile_snapshot.daily_fat_g,
                    daily_carbs_g=None if profile_snapshot is None else profile_snapshot.daily_carbs_g,
                    dietary_notes=dietary_notes,
                )
            )
        return WeeklyMenuGenerationRequest(
            week_start=canonical_week_start,
            dates=tuple(week_dates(canonical_week_start)),
            allowed_meal_slots=tuple(MEAL_SLOT_ORDER),
            locale=str(locale or "ru-RU").strip() or "ru-RU",
            member_count=len(member_snapshots),
            members=tuple(member_snapshots),
            household_dietary_notes=_dedupe_notes(household_notes),
            max_entries=normalized_max_entries,
        )


class AuxiliaryWeeklyMenuGenerator:
    def __init__(
        self,
        *,
        call_llm_fn: Callable[..., object] = safe_call_llm,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 45.0,
        temperature: float = 0.2,
        extra_body: dict[str, object] | None = None,
    ) -> None:
        self._call_llm_fn = call_llm_fn
        self._provider = provider
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = float(timeout)
        self._temperature = float(temperature)
        self._extra_body = dict(extra_body or {})

    def generate(self, request: WeeklyMenuGenerationRequest) -> WeeklyMenuGenerationResponse:
        prompt_payload = {
            "week_start": request.week_start,
            "dates": list(request.dates),
            "allowed_meal_slots": list(request.allowed_meal_slots),
            "locale": request.locale,
            "member_count": request.member_count,
            "members": [
                {
                    "age_band": member.age_band,
                    "daily_kcal_target": member.daily_kcal_target,
                    "daily_protein_g": member.daily_protein_g,
                    "daily_fat_g": member.daily_fat_g,
                    "daily_carbs_g": member.daily_carbs_g,
                    "dietary_notes": list(member.dietary_notes),
                }
                for member in request.members
            ],
            "household_dietary_notes": list(request.household_dietary_notes),
            "max_entries": request.max_entries,
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "Составь недельный план питания и верни только JSON-объект вида "
                    "{\"entries\":[...]}. Не добавляй комментарии, markdown или текст вне JSON. "
                    "Каждая запись должна содержать local_date, meal_slot, position, title и опционально "
                    "description, servings. Используй русский язык. Не придумывай поля вне схемы."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt_payload, ensure_ascii=False, separators=(",", ":")),
            },
        ]
        telemetry = ExternalRequestTelemetry()
        outcome = "provider_failure"
        try:
            response = self._call_llm_fn(
                task="weekly_menu_generation",
                provider=self._provider,
                model=self._model,
                base_url=self._base_url,
                api_key=self._api_key,
                messages=messages,
                temperature=self._temperature,
                timeout=self._timeout,
                extra_body=self._extra_body,
                call_policy=WEEKLY_SINGLE_REQUEST_LLM_CALL_POLICY,
                request_telemetry=telemetry,
            )
            content = extract_content_or_reasoning(response)
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                outcome = "validation_failure"
                raise WeeklyMenuGeneratorValidationError("weekly menu generator returned malformed json") from exc
            result = _parse_generation_response(parsed, request=request)
            outcome = "success"
            return result
        except LLMServiceUnavailableError as exc:
            raise WeeklyMenuGeneratorUnavailableError("weekly menu generator unavailable") from exc
        except WeeklyMenuGeneratorValidationError:
            raise
        except Exception as exc:
            raise WeeklyMenuGeneratorUnavailableError("weekly menu generator unavailable") from exc
        finally:
            logger.info(
                "weekly_menu_provider_call_complete external_request_attempts=%s "
                "external_request_budget=%s outcome=%s retry_performed=%s fallback_performed=%s",
                telemetry.external_request_attempts,
                telemetry.external_request_budget,
                outcome,
                bool(telemetry.retry_performed),
                bool(telemetry.fallback_performed),
            )


def _parse_generation_response(
    payload: object,
    *,
    request: WeeklyMenuGenerationRequest,
) -> WeeklyMenuGenerationResponse:
    if not isinstance(payload, dict):
        raise WeeklyMenuGeneratorValidationError("weekly menu generation payload must be an object")
    if set(payload.keys()) != {"entries"}:
        raise WeeklyMenuGeneratorValidationError("weekly menu generation payload has unknown fields")
    entries_raw = payload.get("entries")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise WeeklyMenuGeneratorValidationError("weekly menu generation payload must include entries")
    if len(entries_raw) > request.max_entries:
        raise WeeklyMenuGeneratorValidationError("weekly menu generation payload exceeds max_entries")
    allowed_dates = set(request.dates)
    allowed_slots = set(request.allowed_meal_slots)
    normalized: list[WeeklyMenuGeneratedEntry] = []
    seen_positions: set[tuple[str, str, int]] = set()
    for entry in entries_raw:
        if not isinstance(entry, dict):
            raise WeeklyMenuGeneratorValidationError("weekly menu generation entry must be an object")
        unknown_fields = set(entry.keys()) - {"local_date", "meal_slot", "position", "title", "description", "servings"}
        missing_fields = {"local_date", "meal_slot", "position", "title"} - set(entry.keys())
        if unknown_fields or missing_fields:
            raise WeeklyMenuGeneratorValidationError("weekly menu generation entry shape is invalid")
        local_date = str(entry["local_date"]).strip()
        meal_slot = str(entry["meal_slot"]).strip()
        try:
            position = int(entry["position"])
        except (TypeError, ValueError) as exc:
            raise WeeklyMenuGeneratorValidationError("weekly menu generation entry position is invalid") from exc
        title = " ".join(str(entry["title"]).split()).strip()
        description = None if entry.get("description") is None else " ".join(str(entry["description"]).split()).strip()
        servings = None if entry.get("servings") is None else " ".join(str(entry["servings"]).split()).strip()
        if local_date not in allowed_dates:
            raise WeeklyMenuGeneratorValidationError("weekly menu generation entry local_date is invalid")
        if meal_slot not in allowed_slots:
            raise WeeklyMenuGeneratorValidationError("weekly menu generation entry meal_slot is invalid")
        if position <= 0:
            raise WeeklyMenuGeneratorValidationError("weekly menu generation entry position is invalid")
        if not title or len(title) > 200:
            raise WeeklyMenuGeneratorValidationError("weekly menu generation entry title is invalid")
        if description == "":
            description = None
        if description is not None and len(description) > 2000:
            raise WeeklyMenuGeneratorValidationError("weekly menu generation entry description is invalid")
        if servings == "":
            servings = None
        if servings is not None and len(servings) > 32:
            raise WeeklyMenuGeneratorValidationError("weekly menu generation entry servings is invalid")
        dedupe_key = (local_date, meal_slot, position)
        if dedupe_key in seen_positions:
            raise WeeklyMenuGeneratorValidationError("weekly menu generation entry positions must be unique")
        seen_positions.add(dedupe_key)
        normalized.append(
            WeeklyMenuGeneratedEntry(
                local_date=local_date,
                meal_slot=meal_slot,
                position=position,
                title=title,
                description=description,
                servings=servings,
            )
        )
    return WeeklyMenuGenerationResponse(entries=tuple(normalized))


class _GenerationCleanupError(RuntimeError):
    pass


class _WeeklyMenuGenerationRuntimeUnavailableError(RuntimeError):
    def __init__(self, result: WeeklyMenuGenerationResult) -> None:
        super().__init__("weekly menu generation runtime unavailable")
        self.result = result


class _WeeklyMenuGenerationAccessError(RuntimeError):
    def __init__(self, result: WeeklyMenuGenerationResult) -> None:
        super().__init__("weekly menu generation access denied")
        self.result = result


@dataclass(frozen=True, slots=True)
class _WeeklyGenerationStateSnapshot:
    week_start: str
    expected_series_version: int | None
    expected_draft_revision_id: str | None
    expected_draft_revision_version: int | None


class HealBiteWeeklyMenuGenerationService:
    def __init__(
        self,
        *,
        generator: WeeklyMenuGenerator,
        member_snapshot_provider: WeeklyMenuMemberSnapshotProvider,
        config: FeatureGateConfig | None = None,
        db_path: str | Path | None = None,
        household_store_factory: HouseholdStoreResourceFactory | None = None,
        weekly_menu_store_factory: WeeklyMenuStoreResourceFactory | None = None,
    ) -> None:
        self._generator = generator
        self._member_snapshot_provider = member_snapshot_provider
        self._config = config if config is not None else load_feature_gate_config("HEALBITE_WEEKLY_MENU")
        self._db_path = resolve_healbite_db_path(db_path)
        self._household_store_factory = household_store_factory or self._default_household_store_factory
        self._weekly_menu_store_factory = weekly_menu_store_factory or self._default_weekly_menu_store_factory

    def _default_household_store_factory(self) -> RuntimeResource[HealBiteHouseholdStore]:
        return borrowed_runtime_resource(HealBiteHouseholdStore(db_path=self._db_path, ensure_schema_on_init=False))

    def _default_weekly_menu_store_factory(self) -> RuntimeResource[HealBiteWeeklyMenuStore]:
        return borrowed_runtime_resource(HealBiteWeeklyMenuStore(db_path=self._db_path))

    def _evaluate_gate(self, actor_user_id: object) -> FeatureGateDecision:
        return evaluate_feature_gate(self._config, actor_user_id)

    def _raise_cleanup_error(self, resource: RuntimeResource[object]) -> None:
        if resource.cleanup_error is not None:
            raise _GenerationCleanupError("weekly menu generation cleanup failure")

    def _resolve_owner_context(
        self,
        actor_user_id: object,
    ) -> HouseholdAuthorizationContext:
        decision = self._evaluate_gate(actor_user_id)
        if not decision.ready:
            raise _WeeklyMenuGenerationRuntimeUnavailableError(_gate_failure_result(decision))
        assert decision.actor_user_id is not None
        resource = self._household_store_factory()
        try:
            with resource as household_store:
                service = HealBiteHouseholdService(household_store)
                context = service.resolve_existing_actor_household_context(decision.actor_user_id)
        except (HouseholdValidationError, HouseholdNotFoundError, HouseholdAccessError, HouseholdIntegrityError, sqlite3.Error):
            raise _WeeklyMenuGenerationRuntimeUnavailableError(
                WeeklyMenuGenerationResult(
                    status=WeeklyMenuGenerationStatus.HOUSEHOLD_UNAVAILABLE,
                    feature_status=FeatureAvailabilityStatus.HOUSEHOLD_UNAVAILABLE,
                )
            ) from None
        self._raise_cleanup_error(resource)
        auth = HouseholdAuthorizationContext.from_household_context(context)
        if (
            auth.member_status is not HouseholdMemberStatus.ACTIVE
            or auth.household_status is not HouseholdStatus.ACTIVE
            or auth.role is not HouseholdRole.OWNER
        ):
            raise _WeeklyMenuGenerationAccessError(WeeklyMenuGenerationResult(status=WeeklyMenuGenerationStatus.FORBIDDEN))
        return auth

    def _capture_generation_state(
        self,
        context: HouseholdAuthorizationContext,
        *,
        week_start: str,
        expected_series_version: int | None,
        payload_hash: str,
        internal_idempotency_key: str,
    ) -> tuple[_WeeklyGenerationStateSnapshot, WeeklyMenuRevisionView | None]:
        resource = self._weekly_menu_store_factory()
        outcome: tuple[_WeeklyGenerationStateSnapshot, WeeklyMenuRevisionView | None]
        try:
            with resource as store:
                if store.schema_state() is not WeeklyMenuSchemaState.CANONICAL:
                    raise _WeeklyMenuGenerationRuntimeUnavailableError(
                        WeeklyMenuGenerationResult(
                            status=WeeklyMenuGenerationStatus.SCHEMA_UNAVAILABLE,
                            feature_status=FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE,
                        )
                    )
                replay = store.lookup_generated_draft_replay(
                    context,
                    idempotency_key=internal_idempotency_key,
                    payload_hash=payload_hash,
                )
                if replay is not None:
                    snapshot = _WeeklyGenerationStateSnapshot(
                        week_start=replay.series.week_start,
                        expected_series_version=replay.series.version,
                        expected_draft_revision_id=replay.revision.id,
                        expected_draft_revision_version=replay.revision.version,
                    )
                    outcome = (snapshot, replay)
                else:
                    series = store.get_weekly_menu_series(context, context.household_id, week_start)
                    if series is None:
                        if expected_series_version is not None:
                            raise WeeklyMenuConflictError("weekly menu series version mismatch")
                        outcome = (
                            _WeeklyGenerationStateSnapshot(
                                week_start=week_start,
                                expected_series_version=None,
                                expected_draft_revision_id=None,
                                expected_draft_revision_version=None,
                            ),
                            None,
                        )
                    else:
                        if expected_series_version is not None and series.version != int(expected_series_version):
                            raise WeeklyMenuConflictError("weekly menu series version mismatch")
                        revisions = store.list_weekly_menu_revisions(context, series.id)
                        current_draft = None
                        for revision in revisions:
                            if revision.status is not WeeklyMenuRevisionStatus.DRAFT:
                                continue
                            if current_draft is not None:
                                raise WeeklyMenuStateError("multiple active draft revisions")
                            current_draft = revision
                        outcome = (
                            _WeeklyGenerationStateSnapshot(
                                week_start=series.week_start,
                                expected_series_version=series.version,
                                expected_draft_revision_id=None if current_draft is None else current_draft.id,
                                expected_draft_revision_version=None if current_draft is None else current_draft.version,
                            ),
                            None,
                        )
        except Exception:
            raise
        self._raise_cleanup_error(resource)
        return outcome

    def _apply_generation_result(
        self,
        context: HouseholdAuthorizationContext,
        *,
        snapshot: _WeeklyGenerationStateSnapshot,
        request: WeeklyMenuGenerationRequest,
        response: WeeklyMenuGenerationResponse,
        internal_idempotency_key: str,
        payload_hash: str,
    ) -> WeeklyMenuRevisionView:
        entries = [
            WeeklyMenuEntryInput(
                local_date=entry.local_date,
                meal_slot=entry.meal_slot,
                position=entry.position,
                title=entry.title,
                description=entry.description,
                servings=entry.servings,
                origin=WeeklyMenuEntryOrigin.GENERATED,
            )
            for entry in response.entries
        ]
        resource = self._weekly_menu_store_factory()
        revision_view: WeeklyMenuRevisionView
        try:
            with resource as store:
                if store.schema_state() is not WeeklyMenuSchemaState.CANONICAL:
                    raise _WeeklyMenuGenerationRuntimeUnavailableError(
                        WeeklyMenuGenerationResult(
                            status=WeeklyMenuGenerationStatus.SCHEMA_UNAVAILABLE,
                            feature_status=FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE,
                        )
                    )
                revision_view = store.apply_generated_draft_entries(
                    context,
                    week_start=request.week_start,
                    entries=entries,
                    expected_series_version=snapshot.expected_series_version,
                    expected_draft_revision_id=snapshot.expected_draft_revision_id,
                    expected_draft_revision_version=snapshot.expected_draft_revision_version,
                    idempotency_key=internal_idempotency_key,
                    payload_hash=payload_hash,
                )
        except Exception:
            raise
        self._raise_cleanup_error(resource)
        return revision_view

    def generate_draft_for_week(
        self,
        actor_user_id: object,
        week_start: str,
        *,
        expected_series_version: int | None = None,
        idempotency_key: str,
        locale: str = "ru-RU",
        max_entries: int = 21,
    ) -> WeeklyMenuGenerationResult:
        try:
            context = self._resolve_owner_context(actor_user_id)
            request = self._member_snapshot_provider.build_request(
                context,
                week_start=week_start,
                locale=locale,
                max_entries=max_entries,
            )
            payload_hash = _request_payload_fingerprint(request)
            internal_idempotency_key = _generation_idempotency_key(idempotency_key)
            snapshot, replay = self._capture_generation_state(
                context,
                week_start=request.week_start,
                expected_series_version=expected_series_version,
                payload_hash=payload_hash,
                internal_idempotency_key=internal_idempotency_key,
            )
            if replay is not None:
                return WeeklyMenuGenerationResult(
                    status=WeeklyMenuGenerationStatus.SUCCESS,
                    revision_view=replay,
                )
            generated = self._generator.generate(request)
            revision_view = self._apply_generation_result(
                context,
                snapshot=snapshot,
                request=request,
                response=generated,
                internal_idempotency_key=internal_idempotency_key,
                payload_hash=payload_hash,
            )
            return WeeklyMenuGenerationResult(
                status=WeeklyMenuGenerationStatus.SUCCESS,
                revision_view=revision_view,
            )
        except _WeeklyMenuGenerationRuntimeUnavailableError as exc:
            return exc.result
        except _WeeklyMenuGenerationAccessError as exc:
            return exc.result
        except _GenerationCleanupError:
            return WeeklyMenuGenerationResult(status=WeeklyMenuGenerationStatus.CLEANUP_FAILURE)
        except WeeklyMenuValidationError:
            return WeeklyMenuGenerationResult(status=WeeklyMenuGenerationStatus.VALIDATION_FAILED)
        except WeeklyMenuConflictError as exc:
            message = str(exc).lower()
            if "idempotency key replayed with different payload" in message:
                return WeeklyMenuGenerationResult(status=WeeklyMenuGenerationStatus.IDEMPOTENCY_CONFLICT)
            return WeeklyMenuGenerationResult(status=WeeklyMenuGenerationStatus.VERSION_CONFLICT)
        except WeeklyMenuStateError:
            return WeeklyMenuGenerationResult(status=WeeklyMenuGenerationStatus.INVALID_STATE)
        except WeeklyMenuGeneratorValidationError:
            return WeeklyMenuGenerationResult(status=WeeklyMenuGenerationStatus.GENERATOR_VALIDATION_FAILED)
        except WeeklyMenuGeneratorUnavailableError:
            return WeeklyMenuGenerationResult(status=WeeklyMenuGenerationStatus.GENERATOR_UNAVAILABLE)
        except sqlite3.Error:
            return WeeklyMenuGenerationResult(status=WeeklyMenuGenerationStatus.STORAGE_FAILURE)
        except Exception:
            return WeeklyMenuGenerationResult(status=WeeklyMenuGenerationStatus.STORAGE_FAILURE)
