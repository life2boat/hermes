"""Per-platform slash command access control.

This module sits beside the existing per-platform allowlist (``allow_from``)
and adds a second axis: of the users who are *allowed to talk to the
gateway*, which ones can run *which slash commands*.

Two lists per platform scope (DM vs group, mirroring ``allow_from`` vs
``group_allow_from``):

  - ``allow_admin_from``      — user IDs that get every registered slash
                                command (built-in + plugin-registered).
  - ``user_allowed_commands`` — slash command names non-admin users may
                                run. Empty / unset → non-admins get no
                                slash commands.

Backward compatibility:

  If ``allow_admin_from`` is not set for a scope, slash command gating
  is disabled entirely for that scope. Every allowed user can run every
  slash command, exactly like before. This means existing installs are
  unaffected until an operator opts in by listing at least one admin.

The gate is applied at the slash command dispatch site in
``gateway/run.py`` so it covers BOTH built-in and plugin-registered
commands via the live registry. Gating slash commands does not affect
plain chat — non-admin users can still talk to the agent normally,
they just can't trigger commands outside ``user_allowed_commands``.

Authored as a slimmed-down salvage of PR #4443's permission tiers
(co-authored by @ReqX). The full tier system, audit log, usage
tracking, rate limiting, and tool filtering from that PR are not
included here — only the slash-command access split.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Iterable, Optional, Tuple


# Commands that require administrative privileges and must never be run by non-admins
# or when admin configuration is absent/empty/invalid.
ADMIN_COMMANDS: FrozenSet[str] = frozenset({
    # System / gateway lifecycle & process control
    "status",
    "restart",
    "stop",
    "platform",
    "platforms",
    "rollback",
    "snapshot",
    "update",
    "debug",
    "agents",
    # Model & configuration
    "model",
    "codex-runtime",
    "codex_runtime",
    "config",
    "yolo",
    "sethome",
    "set-home",
    # Approvals & security
    "approve",
    "deny",
    # Tools, skills, reload
    "reload",
    "reload-mcp",
    "reload_mcp",
    "reload-skills",
    "reload_skills",
    "plugins",
    "tools",
    "toolsets",
    "skills",
    "cron",
    # Memory OS admin
    "memory",
    "memory_stats",
    # Work tracking admin
    "kanban",
})

# Public and user floor commands that must always remain accessible to any authorized
# platform user even when admin gating is enabled with empty user_allowed_commands,
# or when admin gating is unconfigured.
PUBLIC_USER_COMMANDS: FrozenSet[str] = frozenset({
    # Platform discovery / read-only floor
    "help",
    "whoami",
    # HealBite product domain: navigation, nutrition diary, profile, metrics
    "start",
    "menu",
    "diary",
    "stats",
    "undo_meal",
    "diary_undo",
    "weight",
    "water",
    "profile",
})

# Backward-compatibility alias for the floor set
_ALWAYS_ALLOWED_FOR_USERS: FrozenSet[str] = PUBLIC_USER_COMMANDS


@dataclass(frozen=True)
class SlashAccessPolicy:
    """Resolved access policy for a single (platform, scope) pair.

    ``scope`` is ``"dm"`` for direct messages and ``"group"`` for groups,
    channels, threads, and any other multi-user context. The mapping from
    SessionSource.chat_type → scope happens in ``policy_for_source``.
    """

    enabled: bool                      # gating active for this scope?
    admin_user_ids: FrozenSet[str]
    user_allowed_commands: FrozenSet[str]

    def is_admin(self, user_id: Optional[str]) -> bool:
        """Check whether user_id has administrative authority.

        Fail-closed invariant: Administrative privileges strictly require positive
        confirmation in admin_user_ids. Unconfigured, empty, or invalid policies
        never grant administrative authority to any user.
        """
        if not user_id:
            return False
        return str(user_id).strip() in self.admin_user_ids

    def can_run(self, user_id: Optional[str], canonical_cmd: str) -> bool:
        """Check whether user_id is permitted to execute canonical_cmd.

        Rules:
        1. Verified administrators (positive proof in admin_user_ids) can run all commands.
        2. Public and user floor commands (help, whoami, diary, profile, start, menu, etc.)
           are always accessible to any authorized user.
        3. Administrative operations strictly require positive admin authority.
           user_allowed_commands can NEVER authorize any command from ADMIN_COMMANDS.
           When gating is not configured or user is not a verified admin, administrative
           commands fail closed and are denied to everyone.
        4. When gating is enabled, non-admins can only run explicitly allowed user commands
           plus the public floor (excluding any admin commands).
        5. When gating is not configured, standard non-administrative session commands
           (e.g. /new, /retry) remain usable for ordinary chat.
        """
        if not canonical_cmd:
            return False
        cmd = str(canonical_cmd).strip().lstrip("/").lower()
        if not cmd:
            return False

        # 1. Verified administrators can run all commands.
        if self.is_admin(user_id):
            return True

        # 2. Public and user floor commands are always reachable.
        if cmd in PUBLIC_USER_COMMANDS:
            return True

        # 3. Administrative operations strictly require positive admin authorization.
        # user_allowed_commands can NEVER authorize an admin command.
        if cmd in ADMIN_COMMANDS:
            return False

        # 4. When gating is enabled, non-admins are restricted to user_allowed_commands.
        if self.enabled:
            return cmd in self.user_allowed_commands

        # 5. When gating is not configured, standard non-admin commands remain available.
        return True


_DM_CHAT_TYPES = frozenset({"dm", "direct", "private", ""})


def _coerce_id_list(raw: Any) -> FrozenSet[str]:
    """Normalize a YAML-loaded admin/user list into a frozenset of strings.

    Accepts list, tuple, set, comma-separated string, or scalar int/str identifier.
    Safely rejects booleans, mappings, wildcards, and malformed inputs to ensure
    fail-closed security.
    """
    if raw is None or isinstance(raw, bool) or isinstance(raw, (dict, bytes)):
        return frozenset()

    if isinstance(raw, (list, tuple, set, frozenset)):
        items: Iterable[Any] = raw
    elif isinstance(raw, str):
        items = (s for s in raw.split(",") if s.strip())
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        items = (int(raw),)
    else:
        return frozenset()

    out: list[str] = []
    for it in items:
        if it is None or isinstance(it, bool) or isinstance(it, (dict, list, tuple, set, bytes)):
            continue
        s = str(it).strip()
        if not s or s == "*":
            continue
        out.append(s)
    return frozenset(out)


def _coerce_command_list(raw: Any) -> FrozenSet[str]:
    """Normalize a slash command allowlist into a frozenset of lowercase strings.

    Strips leading slashes and whitespace. Safely rejects booleans, mappings,
    and invalid types.
    """
    if raw is None or isinstance(raw, bool) or isinstance(raw, (dict, bytes)):
        return frozenset()

    if isinstance(raw, (list, tuple, set, frozenset)):
        items: Iterable[Any] = raw
    elif isinstance(raw, str):
        items = (s for s in raw.split(",") if s.strip())
    elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
        items = (str(int(raw)),)
    else:
        return frozenset()

    out: list[str] = []
    for it in items:
        if it is None or isinstance(it, bool) or isinstance(it, (dict, list, tuple, set, bytes)):
            continue
        s = str(it).strip().lstrip("/").lower()
        if s:
            out.append(s)
    return frozenset(out)


def _scope_for_chat_type(chat_type: Optional[str]) -> str:
    if chat_type and chat_type.lower() in _DM_CHAT_TYPES:
        return "dm"
    return "group"


def _platform_extra(platform_config: Any) -> dict:
    """Return the ``extra`` dict from a PlatformConfig-like object.

    Defensively handles None and non-PlatformConfig shapes so calling
    code can stay simple.
    """
    if platform_config is None:
        return {}
    extra = getattr(platform_config, "extra", None)
    if isinstance(extra, dict):
        return extra
    if isinstance(platform_config, dict):
        # Some test harnesses pass dicts directly.
        return platform_config
    return {}


def _keys_for_scope(scope: str) -> Tuple[str, str]:
    """Return (admin_key, user_cmd_key) names for a scope."""
    if scope == "group":
        return ("group_allow_admin_from", "group_user_allowed_commands")
    return ("allow_admin_from", "user_allowed_commands")


def policy_from_extra(extra: dict, scope: str) -> SlashAccessPolicy:
    """Build a policy from a platform's ``extra`` dict for one scope.

    DM scope falls back to group scope keys ONLY for ``user_allowed_commands``
    when the DM scope didn't specify its own. This keeps the common case
    (operator wants the same command set DM and group) ergonomic without
    forcing duplication. Admin lists are NOT cross-scope: an admin in
    DMs is not implicitly an admin in a group.
    """
    admin_key, cmd_key = _keys_for_scope(scope)
    admin_ids = _coerce_id_list(extra.get(admin_key))
    raw_cmds = _coerce_command_list(extra.get(cmd_key))

    if scope == "dm" and not raw_cmds:
        # DM didn't specify — let group's user_allowed_commands fall through
        # so operators only need to list it once if it's the same.
        raw_cmds = _coerce_command_list(extra.get("group_user_allowed_commands"))

    # user_allowed_commands can NEVER permit administrative commands.
    # Strip any administrative command to ensure strict fail-closed security.
    cmds = frozenset(c for c in raw_cmds if c not in ADMIN_COMMANDS)

    enabled = bool(admin_ids)
    return SlashAccessPolicy(
        enabled=enabled,
        admin_user_ids=admin_ids,
        user_allowed_commands=cmds,
    )


def policy_for_source(gateway_config: Any, source: Any) -> SlashAccessPolicy:
    """Resolve the access policy for a SessionSource.

    Returns a "disabled" policy (gating off, allow everything) when:
      - gateway_config is None
      - the platform has no PlatformConfig
      - the platform's PlatformConfig has no admin list set for the scope

    Callers should treat the returned policy as authoritative for slash
    command gating only. It does not gate plain chat messages.
    """
    if gateway_config is None or source is None:
        return SlashAccessPolicy(
            enabled=False,
            admin_user_ids=frozenset(),
            user_allowed_commands=frozenset(),
        )
    platforms = getattr(gateway_config, "platforms", None)
    platform_config = None
    if platforms is not None:
        try:
            platform_config = platforms.get(source.platform)
        except Exception:
            platform_config = None
    extra = _platform_extra(platform_config)
    scope = _scope_for_chat_type(getattr(source, "chat_type", None))
    return policy_from_extra(extra, scope)


__all__ = [
    "ADMIN_COMMANDS",
    "PUBLIC_USER_COMMANDS",
    "SlashAccessPolicy",
    "policy_from_extra",
    "policy_for_source",
]
