from __future__ import annotations

import uuid
from enum import Enum

HOUSEHOLDS_TABLE = "households"
HOUSEHOLD_MEMBERS_TABLE = "household_members"
HOUSEHOLD_INVITATIONS_TABLE = "household_invitations"


class HouseholdStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    CLOSED = "closed"


class HouseholdMemberStatus(str, Enum):
    ACTIVE = "active"
    UNLINKED = "unlinked"
    DISABLED = "disabled"
    REMOVED = "removed"


class HouseholdRole(str, Enum):
    OWNER = "owner"
    ADULT_ADMIN = "adult_admin"
    ADULT_MEMBER = "adult_member"
    DEPENDENT = "dependent"


class HouseholdMemberType(str, Enum):
    PRIMARY = "primary"
    LINKED_ADULT = "linked_adult"
    UNLINKED_ADULT = "unlinked_adult"
    DEPENDENT = "dependent"


class HouseholdInvitationStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REFUSED = "refused"
    REVOKED = "revoked"
    EXPIRED = "expired"


HOUSEHOLD_STATUSES = tuple(item.value for item in HouseholdStatus)
HOUSEHOLD_MEMBER_STATUSES = tuple(item.value for item in HouseholdMemberStatus)
HOUSEHOLD_ROLES = tuple(item.value for item in HouseholdRole)
HOUSEHOLD_MEMBER_TYPES = tuple(item.value for item in HouseholdMemberType)
HOUSEHOLD_INVITATION_STATUSES = tuple(item.value for item in HouseholdInvitationStatus)


def new_household_id() -> str:
    return str(uuid.uuid4())


def new_household_member_id() -> str:
    return str(uuid.uuid4())


def new_household_invitation_id() -> str:
    return str(uuid.uuid4())


def is_canonical_uuid4(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 36 or value.lower() != value:
        return False
    try:
        parsed = uuid.UUID(value, version=4)
    except (TypeError, ValueError):
        return False
    return str(parsed) == value and parsed.version == 4


def require_canonical_uuid4(value: str) -> str:
    if not is_canonical_uuid4(value):
        raise ValueError("expected canonical lowercase UUIDv4 text")
    return value
