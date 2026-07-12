from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_family_telegram import (
    FAMILY_ACTION_UNAVAILABLE_REPLY,
    FAMILY_CALLBACK_PREFIX,
    FAMILY_COMMAND,
    FAMILY_MAX_CALLBACK_BYTES,
    FAMILY_PLACEHOLDER_REPLY,
    HealBiteFamilyTelegramController,
    callback_idempotency_key,
    parse_family_callback,
)
from gateway.healbite_household_invitations import (
    HealBiteHouseholdInvitationService,
    HealBiteHouseholdInvitationStore,
    HouseholdInvitationNotFoundError,
)
from gateway.healbite_household_schema import (
    HouseholdInvitationStatus,
    HouseholdMemberStatus,
    HouseholdMemberType,
    HouseholdRole,
    HouseholdStatus,
)
from gateway.healbite_households import (
    HealBiteHouseholdStore,
    HouseholdAccessError,
    HouseholdFeatureConfig,
)
from gateway.platforms.telegram import HEALBITE_REPLY_KEYBOARD_ACTIONS, TelegramAdapter


ACTOR = 101
OTHER_ACTOR = 202
HOUSEHOLD_ID = "11111111-1111-4111-8111-111111111111"
INVITATION_ID = "22222222-2222-4222-8222-222222222222"


def _config(*, enabled: bool = True, allowlist: set[int] | None = None):
    return HouseholdFeatureConfig(
        enabled=enabled,
        allowlist=frozenset({ACTOR} if allowlist is None else allowlist),
        allowlist_valid=True,
    )


def _member(
    *,
    role: HouseholdRole = HouseholdRole.OWNER,
    display_name: str | None = "\u0410\u043b\u0435\u043a\u0441\u0435\u0439 <\u0422\u0435\u0441\u0442>",
    index: int = 1,
):
    return SimpleNamespace(
        id=f"member-{index}",
        household_id=HOUSEHOLD_ID,
        linked_user_id=ACTOR,
        display_name=display_name,
        member_type=HouseholdMemberType.PRIMARY,
        role=role,
        status=HouseholdMemberStatus.ACTIVE,
    )


class _Households:
    def __init__(
        self,
        *,
        role: HouseholdRole = HouseholdRole.OWNER,
        members: list[object] | None = None,
        list_error: Exception | None = None,
    ) -> None:
        self.role = role
        self.members = members if members is not None else [_member(role=role)]
        self.list_error = list_error

    def resolve_existing_actor_household_context(self, actor):
        assert actor == ACTOR
        return SimpleNamespace(
            actor_user_id=actor,
            household_id=HOUSEHOLD_ID,
            household_member_id="member-1",
            role=self.role,
            member_status=HouseholdMemberStatus.ACTIVE,
            household_status=HouseholdStatus.ACTIVE,
        )

    def get_actor_household(self, actor):
        assert actor == ACTOR
        return SimpleNamespace(
            id=HOUSEHOLD_ID,
            name="\u0414\u043e\u043c & \u0441\u0435\u043c\u044c\u044f",
            status=HouseholdStatus.ACTIVE,
        )

    def get_membership_for_actor(self, actor, household_id):
        assert actor == ACTOR and household_id == HOUSEHOLD_ID
        return _member(role=self.role)

    def list_members_for_actor(self, actor, household_id):
        assert actor == ACTOR and household_id == HOUSEHOLD_ID
        if self.list_error:
            raise self.list_error
        return self.members


def _invitation(*, invitation_id: str = INVITATION_ID):
    return SimpleNamespace(
        id=invitation_id,
        household_id=HOUSEHOLD_ID,
        invitee_user_id=ACTOR,
        invited_by_user_id=OTHER_ACTOR,
        proposed_role=HouseholdRole.ADULT_MEMBER,
        status=HouseholdInvitationStatus.PENDING,
        expires_at="2026-07-20T12:00:00.000000Z",
    )


class _Invitations:
    def __init__(
        self,
        invitations: list[object] | None = None,
        *,
        fail_mutation: bool = False,
    ) -> None:
        self.items = list(invitations or [])
        self.fail_mutation = fail_mutation
        self.accept_keys: list[str] = []
        self.refuse_keys: list[str] = []

    def list_pending_invitations_for_actor(self, actor):
        assert actor == ACTOR
        return list(self.items)

    def accept_invitation(self, actor, invitation_id, idempotency_key):
        if self.fail_mutation or actor != ACTOR or invitation_id != INVITATION_ID:
            raise HouseholdInvitationNotFoundError("invitation unavailable")
        self.accept_keys.append(idempotency_key)
        return SimpleNamespace(invitation=_invitation(), household_member_id="opaque")

    def refuse_invitation(self, actor, invitation_id, idempotency_key):
        if self.fail_mutation or actor != ACTOR or invitation_id != INVITATION_ID:
            raise HouseholdInvitationNotFoundError("invitation unavailable")
        self.refuse_keys.append(idempotency_key)
        self.items = []
        return _invitation()


def _controller(
    *,
    enabled: bool = True,
    role: HouseholdRole = HouseholdRole.OWNER,
    households: _Households | None = None,
    invitations: _Invitations | None = None,
):
    household_service = households or _Households(role=role)
    invitation_service = invitations or _Invitations()
    return HealBiteFamilyTelegramController(
        config=_config(enabled=enabled),
        household_service_factory=lambda: household_service,
        invitation_service_factory=lambda: invitation_service,
    )


def _family_button_label() -> str:
    return next(label for label, action in HEALBITE_REPLY_KEYBOARD_ACTIONS.items() if action == FAMILY_COMMAND)


def _message(*, text: str, user_id: int = ACTOR):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=user_id, username="ignored", first_name="Ignored"),
        chat=SimpleNamespace(id=555, type="private"),
        chat_id=555,
        message_thread_id=None,
    )


def _query(*, data: str, user_id: int = ACTOR, query_id: str = "query-one"):
    message = _message(text="old", user_id=user_id)
    return SimpleNamespace(
        id=query_id,
        data=data,
        from_user=SimpleNamespace(id=user_id, username="ignored", first_name="Ignored"),
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def _adapter(controller):
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={}))
    adapter._family_telegram = controller
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._enqueue_text_event = Mock()
    adapter.handle_message = AsyncMock()
    adapter._should_process_message = lambda msg, is_command=False: True
    return adapter


def test_disabled_and_non_allowlisted_gate_fail_before_services():
    def unexpected():
        raise AssertionError("service must not be opened")

    disabled = HealBiteFamilyTelegramController(
        config=_config(enabled=False),
        household_service_factory=unexpected,
        invitation_service_factory=unexpected,
    )
    foreign = HealBiteFamilyTelegramController(
        config=_config(allowlist={OTHER_ACTOR}),
        household_service_factory=unexpected,
        invitation_service_factory=unexpected,
    )

    assert disabled.home(ACTOR).screen.text == FAMILY_PLACEHOLDER_REPLY
    assert disabled.handle_callback(ACTOR, f"{FAMILY_CALLBACK_PREFIX}home", callback_query_id="q").state == "disabled"
    assert foreign.home(ACTOR).state == "disabled"


@pytest.mark.parametrize(
    "role,member_button",
    [
        (HouseholdRole.OWNER, True),
        (HouseholdRole.ADULT_ADMIN, True),
        (HouseholdRole.ADULT_MEMBER, True),
        (HouseholdRole.DEPENDENT, False),
    ],
)
def test_family_home_localizes_roles_and_applies_member_policy(role, member_button):
    result = _controller(role=role).home(ACTOR)

    assert result.state == "home"
    assert HOUSEHOLD_ID not in result.screen.text
    assert str(ACTOR) not in result.screen.text
    assert "&amp;" in result.screen.text
    callbacks = [item for row in result.screen.rows for item in row]
    assert any(label == "\u0423\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u0438" for label, _data in callbacks) is member_button
    assert role.value not in result.screen.text


def test_member_list_escapes_names_hides_ids_and_paginates():
    members = [
        _member(display_name=f"\u0423\u0447\u0430\u0441\u0442\u043d\u0438\u043a <{index}>", index=index)
        for index in range(1, 24)
    ]
    controller = _controller(households=_Households(members=members))

    first = controller.members(ACTOR)
    second = controller.handle_callback(
        ACTOR,
        f"{FAMILY_CALLBACK_PREFIX}members:1",
        callback_query_id="page",
    )

    assert first.state == "members"
    assert "&lt;1&gt;" in first.screen.text
    assert HOUSEHOLD_ID not in first.screen.text
    assert len(first.screen.text) < 3500
    assert any(data.endswith("members:1") for row in first.screen.rows for _label, data in row)
    assert "\u0423\u0447\u0430\u0441\u0442\u043d\u0438\u043a &lt;21&gt;" in second.screen.text


def test_dependent_member_list_is_denied_server_side():
    households = _Households(
        role=HouseholdRole.DEPENDENT,
        list_error=HouseholdAccessError("household access denied"),
    )

    result = _controller(households=households).members(ACTOR)

    assert result.state == "denied"
    assert result.screen.text == "\u0421\u043f\u0438\u0441\u043e\u043a \u0443\u0447\u0430\u0441\u0442\u043d\u0438\u043a\u043e\u0432 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d."


def test_incoming_invitation_is_safe_and_callback_fits_telegram_limit():
    result = _controller(invitations=_Invitations([_invitation()])).invitations(ACTOR)

    assert result.state == "invitations"
    assert "\u041f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u0432 \u0441\u0435\u043c\u044c\u044e" in result.screen.text
    assert INVITATION_ID not in result.screen.text
    assert HOUSEHOLD_ID not in result.screen.text
    assert str(ACTOR) not in result.screen.text
    callbacks = [data for row in result.screen.rows for _label, data in row]
    assert all(len(data.encode("utf-8")) <= FAMILY_MAX_CALLBACK_BYTES for data in callbacks)
    assert any(data.endswith(INVITATION_ID) for data in callbacks)


def test_empty_incoming_invitation_list_has_no_mutation_buttons():
    result = _controller().invitations(ACTOR)

    assert "\u041d\u043e\u0432\u044b\u0445 \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0439 \u043d\u0435\u0442." in result.screen.text
    assert not any(
        label in {"\u041f\u0440\u0438\u043d\u044f\u0442\u044c", "\u041e\u0442\u043a\u0430\u0437\u0430\u0442\u044c\u0441\u044f"}
        for row in result.screen.rows
        for label, _data in row
    )


def test_accept_callback_uses_stable_idempotency_and_returns_home():
    invitations = _Invitations([_invitation()])
    controller = _controller(invitations=invitations)
    data = f"{FAMILY_CALLBACK_PREFIX}accept:{INVITATION_ID}"

    first = controller.handle_callback(ACTOR, data, callback_query_id="same-query")
    second = controller.handle_callback(ACTOR, data, callback_query_id="same-query")

    assert first.state == second.state == "home"
    assert invitations.accept_keys[0] == invitations.accept_keys[1]
    assert "\u041f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u0438\u0435 \u043f\u0440\u0438\u043d\u044f\u0442\u043e." in first.screen.text
    assert INVITATION_ID not in first.screen.text


def test_refuse_callback_is_actor_scoped_and_refreshes_list():
    invitations = _Invitations([_invitation()])
    controller = _controller(invitations=invitations)

    result = controller.handle_callback(
        ACTOR,
        f"{FAMILY_CALLBACK_PREFIX}refuse:{INVITATION_ID}",
        callback_query_id="refuse-query",
    )

    assert result.state == "invitations"
    assert invitations.items == []
    assert invitations.refuse_keys == [
        callback_idempotency_key("refuse-query", action="refuse")
    ]


@pytest.mark.parametrize(
    "data",
    [
        "family:v2:home",
        "family:v1:unknown",
        "family:v1:accept",
        "family:v1:members:not-a-page",
        "family:v1:" + "x" * 80,
    ],
)
def test_malformed_and_unknown_callbacks_fail_closed(data):
    result = _controller().handle_callback(ACTOR, data, callback_query_id="q")

    assert result.state == "stale"
    assert result.screen.text == FAMILY_ACTION_UNAVAILABLE_REPLY


def test_foreign_and_random_invitation_references_are_indistinguishable():
    controller = _controller(invitations=_Invitations(fail_mutation=True))

    foreign = controller.handle_callback(
        ACTOR,
        f"{FAMILY_CALLBACK_PREFIX}accept:{INVITATION_ID}",
        callback_query_id="foreign",
    )
    random = controller.handle_callback(
        ACTOR,
        f"{FAMILY_CALLBACK_PREFIX}accept:33333333-3333-4333-8333-333333333333",
        callback_query_id="random",
    )

    assert foreign == random


@pytest.mark.asyncio
async def test_family_button_and_command_share_local_handler_when_disabled():
    adapter = _adapter(_controller(enabled=False))

    button_update = SimpleNamespace(
        update_id=1,
        message=_message(text=_family_button_label()),
        effective_message=None,
    )
    command_update = SimpleNamespace(
        update_id=2,
        message=_message(text=FAMILY_COMMAND),
        effective_message=None,
    )

    assert await adapter._maybe_handle_healbite_menu_button(button_update, SimpleNamespace()) is True
    await adapter._handle_command(command_update, SimpleNamespace())

    assert adapter._send_message_with_thread_fallback.await_count == 2
    assert {
        call.kwargs["text"]
        for call in adapter._send_message_with_thread_fallback.await_args_list
    } == {FAMILY_PLACEHOLDER_REPLY}
    adapter._enqueue_text_event.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_family_callback_is_consumed_locally_and_never_generic_dispatch(caplog):
    adapter = _adapter(_controller())
    query = _query(data="family:v9:forged")
    update = SimpleNamespace(callback_query=query)

    await adapter._handle_callback_query(update, SimpleNamespace())

    query.answer.assert_awaited_once()
    query.edit_message_text.assert_awaited_once()
    adapter.handle_message.assert_not_awaited()
    adapter._enqueue_text_event.assert_not_called()
    assert INVITATION_ID not in caplog.text
    assert HOUSEHOLD_ID not in caplog.text
    assert str(ACTOR) not in caplog.text


@pytest.mark.asyncio
async def test_family_callback_edit_failure_uses_safe_send_fallback():
    adapter = _adapter(_controller())
    query = _query(data=f"{FAMILY_CALLBACK_PREFIX}home")
    query.edit_message_text.side_effect = RuntimeError("synthetic edit failure")

    await adapter._handle_healbite_family_callback(query, query.data)

    adapter._send_message_with_thread_fallback.assert_awaited_once()
    sent = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "\u0414\u043e\u043c &amp; \u0441\u0435\u043c\u044c\u044f" in sent["text"]

@pytest.mark.asyncio
async def test_family_back_callback_returns_to_main_menu():
    adapter = _adapter(_controller())
    query = _query(data=f"{FAMILY_CALLBACK_PREFIX}back")

    await adapter._handle_healbite_family_callback(query, query.data)

    query.answer.assert_awaited_once()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    adapter._send_message_with_thread_fallback.assert_awaited_once()
    assert adapter._send_message_with_thread_fallback.await_args.kwargs["reply_markup"] is not None
    adapter.handle_message.assert_not_awaited()



def test_callback_parser_and_constructor_contract():
    assert parse_family_callback(f"{FAMILY_CALLBACK_PREFIX}home") == ("home", None)
    assert parse_family_callback(f"{FAMILY_CALLBACK_PREFIX}invites:2") == ("invites", "2")
    assert parse_family_callback("family:v2:home") is None
    assert len(callback_idempotency_key("query", action="accept")) <= 128


def _create_users(db_path: Path, *user_ids: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT)"
        )
        conn.executemany(
            "INSERT INTO users (user_id, username) VALUES (?, ?)",
            [(user_id, "synthetic") for user_id in user_ids],
        )


def test_real_actor_scoped_services_accept_once_and_refresh_home(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users(db_path, ACTOR, OTHER_ACTOR)
    household_store = HealBiteHouseholdStore(db_path)
    owner_household = household_store.get_or_create_personal_household(OTHER_ACTOR)
    invitation_service = HealBiteHouseholdInvitationService(
        HealBiteHouseholdInvitationStore(db_path)
    )
    invitation = invitation_service.create_household_invitation(
        OTHER_ACTOR,
        owner_household.household.id,
        ACTOR,
        HouseholdRole.ADULT_MEMBER,
        datetime.now(timezone.utc) + timedelta(days=1),
        "create-family-ui-integration",
    )
    controller = HealBiteFamilyTelegramController(
        config=_config(),
        db_path=db_path,
    )
    data = f"{FAMILY_CALLBACK_PREFIX}accept:{invitation.id}"

    pending = controller.invitations(ACTOR)
    accepted = controller.handle_callback(
        ACTOR,
        data,
        callback_query_id="same-real-query",
    )
    duplicate = controller.handle_callback(
        ACTOR,
        data,
        callback_query_id="same-real-query",
    )

    assert pending.state == "invitations"
    assert invitation.id not in pending.screen.text
    assert accepted.state == duplicate.state == "home"
    assert household_store.resolve_existing_actor_context(ACTOR).household_id == owner_household.household.id
