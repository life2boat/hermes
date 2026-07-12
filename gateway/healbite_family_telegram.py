from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Callable

from gateway.healbite_household_invitations import (
    HealBiteHouseholdInvitationService,
    HealBiteHouseholdInvitationStore,
    HouseholdInvitationError,
    HouseholdInvitationValidationError,
)
from gateway.healbite_household_schema import HouseholdRole
from gateway.healbite_households import (
    HealBiteHouseholdService,
    HealBiteHouseholdStore,
    HouseholdAccessError,
    HouseholdFeatureConfig,
    HouseholdIntegrityError,
    HouseholdNotFoundError,
    HouseholdValidationError,
    load_household_feature_config,
)
from gateway.healbite_nutrition_diary import resolve_healbite_db_path

FAMILY_COMMAND = "/family"
FAMILY_CALLBACK_ROOT = "family:"
FAMILY_CALLBACK_PREFIX = "family:v1:"
FAMILY_PLACEHOLDER_REPLY = "\u0412 \u0440\u0430\u0437\u0440\u0430\u0431\u043e\u0442\u043a\u0435"
FAMILY_UNAVAILABLE_REPLY = "\u0420\u0430\u0437\u0434\u0435\u043b \u0441\u0435\u043c\u044c\u0438 \u0441\u0435\u0439\u0447\u0430\u0441 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d."
FAMILY_ACTION_UNAVAILABLE_REPLY = "\u0414\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u043e. \u041e\u0431\u043d\u043e\u0432\u0438\u0442\u0435 \u0440\u0430\u0437\u0434\u0435\u043b \u0441\u0435\u043c\u044c\u0438."
FAMILY_MAX_CALLBACK_BYTES = 64
FAMILY_MEMBER_PAGE_SIZE = 20
FAMILY_INVITATION_PAGE_SIZE = 5

_READ_MEMBER_ROLES = frozenset(
    {HouseholdRole.OWNER, HouseholdRole.ADULT_ADMIN, HouseholdRole.ADULT_MEMBER}
)
_ROLE_LABELS = {
    HouseholdRole.OWNER: "\u0412\u043b\u0430\u0434\u0435\u043b\u0435\u0446",
    HouseholdRole.ADULT_ADMIN: "\u0410\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440",
    HouseholdRole.ADULT_MEMBER: "\u0423\u0447\u0430\u0441\u0442\u043d\u0438\u043a",
    HouseholdRole.DEPENDENT: "\u041f\u043e\u0434\u043e\u043f\u0435\u0447\u043d\u044b\u0439",
}


@dataclass(frozen=True, slots=True)
class FamilyTelegramScreen:
    text: str
    rows: tuple[tuple[tuple[str, str], ...], ...] = ()
    parse_mode: str | None = "HTML"


@dataclass(frozen=True, slots=True)
class FamilyTelegramResult:
    state: str
    screen: FamilyTelegramScreen
    notice: str | None = None
    error_class: str | None = None


HouseholdServiceFactory = Callable[[], HealBiteHouseholdService]
InvitationServiceFactory = Callable[[], HealBiteHouseholdInvitationService]


def _positive_actor(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        actor = int(value)
    except (TypeError, ValueError):
        return None
    return actor if 0 < actor <= 2**63 - 1 else None


def _safe_label(value: object, *, fallback: str, limit: int = 100) -> str:
    collapsed = " ".join(str(value or "").split())
    if not collapsed:
        collapsed = fallback
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1].rstrip() + "\u2026"
    return escape(collapsed)


def _role_label(role: HouseholdRole) -> str:
    return _ROLE_LABELS.get(role, "\u0423\u0447\u0430\u0441\u0442\u043d\u0438\u043a")


def _expiry_label(value: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "\u0441\u0440\u043e\u043a \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d"
    return parsed.strftime("\u0434\u043e %d.%m.%Y %H:%M UTC")


def _callback(action: str, argument: str | int | None = None) -> str:
    data = f"{FAMILY_CALLBACK_PREFIX}{action}"
    if argument is not None:
        data = f"{data}:{argument}"
    if len(data.encode("utf-8")) > FAMILY_MAX_CALLBACK_BYTES:
        raise ValueError("family callback is too long")
    return data


def parse_family_callback(data: object) -> tuple[str, str | None] | None:
    if not isinstance(data, str) or len(data.encode("utf-8")) > FAMILY_MAX_CALLBACK_BYTES:
        return None
    if not data.startswith(FAMILY_CALLBACK_PREFIX):
        return None
    payload = data[len(FAMILY_CALLBACK_PREFIX) :]
    action, separator, argument = payload.partition(":")
    if action in {"home", "back", "members", "invites"}:
        if action in {"home", "back"} and separator:
            return None
        if action != "home" and separator:
            if not argument.isdigit() or int(argument) > 10000:
                return None
            return action, argument
        return action, None
    if action in {"accept", "refuse"} and separator and argument:
        return action, argument
    return None


def callback_idempotency_key(callback_query_id: object, *, action: str) -> str:
    normalized = str(callback_query_id or "").strip()
    digest = hashlib.sha256(f"{action}:{normalized}".encode("utf-8")).hexdigest()
    return f"telegram-family:{digest}"


class HealBiteFamilyTelegramController:
    def __init__(
        self,
        *,
        config: HouseholdFeatureConfig | None = None,
        db_path: str | Path | None = None,
        household_service_factory: HouseholdServiceFactory | None = None,
        invitation_service_factory: InvitationServiceFactory | None = None,
    ) -> None:
        self._config = config if config is not None else load_household_feature_config()
        self._db_path = resolve_healbite_db_path(db_path)
        self._household_service_factory = household_service_factory or self._household_service
        self._invitation_service_factory = invitation_service_factory or self._invitation_service

    def _household_service(self) -> HealBiteHouseholdService:
        return HealBiteHouseholdService(
            HealBiteHouseholdStore(self._db_path, ensure_schema_on_init=False)
        )

    def _invitation_service(self) -> HealBiteHouseholdInvitationService:
        return HealBiteHouseholdInvitationService(
            HealBiteHouseholdInvitationStore(self._db_path, ensure_schema_on_init=False)
        )

    def _eligible_actor(self, actor_user_id: object) -> int | None:
        actor = _positive_actor(actor_user_id)
        if (
            actor is None
            or not self._config.enabled
            or not self._config.allowlist_valid
            or actor not in self._config.allowlist
        ):
            return None
        return actor

    @property
    def feature_enabled(self) -> bool:
        return bool(self._config.enabled and self._config.allowlist_valid)

    def _placeholder(self) -> FamilyTelegramResult:
        return FamilyTelegramResult(
            state="disabled",
            screen=FamilyTelegramScreen(FAMILY_PLACEHOLDER_REPLY, parse_mode=None),
        )

    def _unavailable(self, *, error_class: str = "unavailable") -> FamilyTelegramResult:
        return FamilyTelegramResult(
            state="unavailable",
            screen=FamilyTelegramScreen(
                FAMILY_UNAVAILABLE_REPLY,
                rows=((("\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c", _callback("home")),),),
                parse_mode=None,
            ),
            error_class=error_class,
        )

    def home(self, actor_user_id: object, *, notice: str | None = None) -> FamilyTelegramResult:
        actor = self._eligible_actor(actor_user_id)
        if actor is None:
            return self._placeholder()
        try:
            service = self._household_service_factory()
            context = service.resolve_existing_actor_household_context(actor)
            household = service.get_actor_household(actor)
            membership = service.get_membership_for_actor(actor, context.household_id)
        except HouseholdNotFoundError:
            return FamilyTelegramResult(
                state="empty",
                screen=FamilyTelegramScreen(
                    "<b>\u0421\u0435\u043c\u044c\u044f</b>\n\n\u0421\u0435\u043c\u044c\u044f \u043f\u043e\u043a\u0430 \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d\u0430.",
                    rows=(
                        (("\u0412\u0445\u043e\u0434\u044f\u0449\u0438\u0435 \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u044f", _callback("invites")),),
                        (("\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c", _callback("home")), ("\u041d\u0430\u0437\u0430\u0434", _callback("back"))),
                    ),
                ),
                notice=notice,
            )
        except HouseholdValidationError:
            return self._unavailable(error_class="actor_unavailable")
        except HouseholdAccessError:
            return self._unavailable(error_class="access_denied")
        except (HouseholdIntegrityError, sqlite3.Error):
            return self._unavailable(error_class="state_unavailable")
        except Exception:
            return self._unavailable(error_class="internal_error")

        household_label = _safe_label(household.name, fallback="\u041c\u043e\u044f \u0441\u0435\u043c\u044c\u044f")
        role_label = _role_label(membership.role)
        rows: list[tuple[tuple[str, str], ...]] = []
        if context.role in _READ_MEMBER_ROLES:
            rows.append((("\u0423\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u0438", _callback("members")),))
        rows.extend(
            [
                (("\u0412\u0445\u043e\u0434\u044f\u0449\u0438\u0435 \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u044f", _callback("invites")),),
                (("\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c", _callback("home")), ("\u041d\u0430\u0437\u0430\u0434", _callback("back"))),
            ]
        )
        prefix = f"{escape(notice)}\n\n" if notice else ""
        return FamilyTelegramResult(
            state="home",
            screen=FamilyTelegramScreen(
                f"{prefix}<b>{household_label}</b>\n"
                f"\u0412\u0430\u0448\u0430 \u0440\u043e\u043b\u044c: {role_label}\n"
                "\u0421\u0442\u0430\u0442\u0443\u0441: \u0430\u043a\u0442\u0438\u0432\u043d\u0430",
                rows=tuple(rows),
            ),
        )

    def members(self, actor_user_id: object, *, page: int = 0) -> FamilyTelegramResult:
        actor = self._eligible_actor(actor_user_id)
        if actor is None:
            return self._placeholder()
        try:
            service = self._household_service_factory()
            context = service.resolve_existing_actor_household_context(actor)
            members = service.list_members_for_actor(actor, context.household_id)
        except HouseholdAccessError:
            return FamilyTelegramResult(
                state="denied",
                screen=FamilyTelegramScreen(
                    "\u0421\u043f\u0438\u0441\u043e\u043a \u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u043e\u0432 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d.",
                    rows=((("\u041d\u0430\u0437\u0430\u0434", _callback("home")),),),
                    parse_mode=None,
                ),
                error_class="access_denied",
            )
        except (HouseholdNotFoundError, HouseholdValidationError):
            return self._unavailable(error_class="actor_unavailable")
        except (HouseholdIntegrityError, sqlite3.Error):
            return self._unavailable(error_class="state_unavailable")
        except Exception:
            return self._unavailable(error_class="internal_error")

        active_members = [item for item in members if item.status.value == "active"]
        last_page = max(0, (len(active_members) - 1) // FAMILY_MEMBER_PAGE_SIZE)
        page = max(0, min(int(page), last_page))
        start = page * FAMILY_MEMBER_PAGE_SIZE
        visible = active_members[start : start + FAMILY_MEMBER_PAGE_SIZE]
        lines = ["<b>\u0423\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u0438 \u0441\u0435\u043c\u044c\u0438</b>"]
        for index, member in enumerate(visible, start=start + 1):
            label = _safe_label(member.display_name, fallback=f"\u0423\u0447\u0430\u0441\u0442\u043d\u0438\u043a {index}", limit=80)
            lines.append(f"{index}. {label} \u2014 {_role_label(member.role)}")
        if not visible:
            lines.append("\u0421\u043f\u0438\u0441\u043e\u043a \u043f\u0443\u0441\u0442.")
        rows: list[tuple[tuple[str, str], ...]] = []
        paging: list[tuple[str, str]] = []
        if page > 0:
            paging.append(("\u041d\u0430\u0437\u0430\u0434", _callback("members", page - 1)))
        if page < last_page:
            paging.append(("\u0414\u0430\u043b\u0435\u0435", _callback("members", page + 1)))
        if paging:
            rows.append(tuple(paging))
        rows.append((("\u041a \u0440\u0430\u0437\u0434\u0435\u043b\u0443 \u0441\u0435\u043c\u044c\u0438", _callback("home")),))
        return FamilyTelegramResult(
            state="members",
            screen=FamilyTelegramScreen("\n".join(lines), rows=tuple(rows)),
        )

    def invitations(self, actor_user_id: object, *, page: int = 0) -> FamilyTelegramResult:
        actor = self._eligible_actor(actor_user_id)
        if actor is None:
            return self._placeholder()
        try:
            invitations = self._invitation_service_factory().list_pending_invitations_for_actor(actor)
        except (HouseholdInvitationError, HouseholdInvitationValidationError, sqlite3.Error):
            return self._unavailable(error_class="invitation_unavailable")
        except Exception:
            return self._unavailable(error_class="internal_error")

        last_page = max(0, (len(invitations) - 1) // FAMILY_INVITATION_PAGE_SIZE)
        page = max(0, min(int(page), last_page))
        start = page * FAMILY_INVITATION_PAGE_SIZE
        visible = invitations[start : start + FAMILY_INVITATION_PAGE_SIZE]
        lines = ["<b>\u0412\u0445\u043e\u0434\u044f\u0449\u0438\u0435 \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u044f</b>"]
        rows: list[tuple[tuple[str, str], ...]] = []
        for index, invitation in enumerate(visible, start=start + 1):
            lines.extend(
                [
                    "",
                    f"{index}. \u041f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u0432 \u0441\u0435\u043c\u044c\u044e",
                    f"\u0420\u043e\u043b\u044c: {_role_label(invitation.proposed_role)}",
                    f"\u0414\u0435\u0439\u0441\u0442\u0432\u0443\u0435\u0442 {_expiry_label(invitation.expires_at)}",
                ]
            )
            rows.append(
                (
                    ("\u041f\u0440\u0438\u043d\u044f\u0442\u044c", _callback("accept", invitation.id)),
                    ("\u041e\u0442\u043a\u0430\u0437\u0430\u0442\u044c\u0441\u044f", _callback("refuse", invitation.id)),
                )
            )
        if not visible:
            lines.append("")
            lines.append("\u041d\u043e\u0432\u044b\u0445 \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0439 \u043d\u0435\u0442.")
        paging: list[tuple[str, str]] = []
        if page > 0:
            paging.append(("\u041d\u0430\u0437\u0430\u0434", _callback("invites", page - 1)))
        if page < last_page:
            paging.append(("\u0414\u0430\u043b\u0435\u0435", _callback("invites", page + 1)))
        if paging:
            rows.append(tuple(paging))
        rows.append((("\u041a \u0440\u0430\u0437\u0434\u0435\u043b\u0443 \u0441\u0435\u043c\u044c\u0438", _callback("home")),))
        return FamilyTelegramResult(
            state="invitations",
            screen=FamilyTelegramScreen("\n".join(lines), rows=tuple(rows)),
        )

    def handle_callback(
        self,
        actor_user_id: object,
        callback_data: object,
        *,
        callback_query_id: object,
    ) -> FamilyTelegramResult:
        actor = self._eligible_actor(actor_user_id)
        if actor is None:
            return self._placeholder()
        parsed = parse_family_callback(callback_data)
        if parsed is None:
            return FamilyTelegramResult(
                state="stale",
                screen=FamilyTelegramScreen(FAMILY_ACTION_UNAVAILABLE_REPLY, parse_mode=None),
                error_class="invalid_callback",
            )
        action, argument = parsed
        if action == "home":
            return self.home(actor)
        if action == "back":
            return FamilyTelegramResult(
                state="back",
                screen=FamilyTelegramScreen("", parse_mode=None),
            )
        if action == "members":
            return self.members(actor, page=int(argument or 0))
        if action == "invites":
            return self.invitations(actor, page=int(argument or 0))
        assert argument is not None
        key = callback_idempotency_key(callback_query_id, action=action)
        try:
            service = self._invitation_service_factory()
            if action == "accept":
                service.accept_invitation(actor, argument, key)
                return self.home(actor, notice="\u041f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u043f\u0440\u0438\u043d\u044f\u0442\u043e.")
            service.refuse_invitation(actor, argument, key)
            result = self.invitations(actor)
            return FamilyTelegramResult(
                state=result.state,
                screen=result.screen,
                notice="\u041f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u043e.",
            )
        except (HouseholdInvitationError, HouseholdInvitationValidationError):
            return FamilyTelegramResult(
                state="stale",
                screen=FamilyTelegramScreen(
                    FAMILY_ACTION_UNAVAILABLE_REPLY,
                    rows=((("\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u044f", _callback("invites")),),),
                    parse_mode=None,
                ),
                error_class="invitation_unavailable",
            )
        except sqlite3.Error:
            return self._unavailable(error_class="state_unavailable")
        except Exception:
            return self._unavailable(error_class="internal_error")


def build_family_telegram_controller(
    *,
    env: dict[str, str] | None = None,
    db_path: str | Path | None = None,
) -> HealBiteFamilyTelegramController:
    return HealBiteFamilyTelegramController(
        config=load_household_feature_config(env),
        db_path=db_path,
    )
