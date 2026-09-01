"""Deterministic operator rendering: canonical JSON and human summary (PR-12).

Same authoritative input state -> same serialized output bytes. No wall
clock, randomness, object reprs, or unbounded structures are introduced
during rendering. The redaction policy is applied before anything
crosses the operator boundary.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from ai_engineering.observability.contracts import OBSERVABILITY_SCHEMA_VERSION
from ai_engineering.observability.projection import OperatorSnapshot
from ai_engineering.observability.redaction import redact_operator_dict


class ObservabilitySchemaError(ValueError):
    """Fail-closed error for unsupported operator snapshot schema versions."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def load_operator_snapshot_dict(raw: str | bytes) -> dict[str, Any]:
    """Parse and validate an operator snapshot dictionary (fail closed)."""

    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ObservabilitySchemaError("OBSERVABILITY_SCHEMA_UNSUPPORTED") from exc
    if not raw or len(raw) > 64 * 1024 * 1024:
        raise ObservabilitySchemaError("OBSERVABILITY_SCHEMA_UNSUPPORTED")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ObservabilitySchemaError("OBSERVABILITY_SCHEMA_UNSUPPORTED") from exc
    if not isinstance(payload, Mapping):
        raise ObservabilitySchemaError("OBSERVABILITY_SCHEMA_UNSUPPORTED")
    version = payload.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ObservabilitySchemaError("OBSERVABILITY_SCHEMA_UNSUPPORTED")
    if version > OBSERVABILITY_SCHEMA_VERSION:
        raise ObservabilitySchemaError("OBSERVABILITY_SCHEMA_UNSUPPORTED")
    return dict(payload)


def _apply_redaction(records: list[tuple[str, str]], new_records: tuple[tuple[str, str], ...]) -> None:
    records.extend(new_records)


def redacted_snapshot_dict(snapshot: OperatorSnapshot | Mapping[str, Any]) -> dict[str, Any]:
    """Return the redacted canonical dictionary for an operator snapshot."""

    if isinstance(snapshot, OperatorSnapshot):
        base = snapshot.to_dict()
    else:
        base = dict(snapshot)
    redacted, records = redact_operator_dict(base)
    existing = redacted.get("redactions")
    merged: list[tuple[str, str]] = []
    if isinstance(existing, list):
        for record in existing:
            if isinstance(record, Mapping):
                merged.append((str(record.get("field_path", "")), str(record.get("code", ""))))
    merged.extend(records)
    deduped = sorted(set(merged))
    redacted["redactions"] = [
        {"field_path": path, "code": code, "disclosure": "SUPPRESSED"} for path, code in deduped
    ]
    return redacted


def canonical_json(snapshot: OperatorSnapshot | Mapping[str, Any]) -> str:
    """Render the operator snapshot as deterministic, redacted canonical JSON."""

    redacted = redacted_snapshot_dict(snapshot)
    return json.dumps(redacted, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def human_summary(snapshot: OperatorSnapshot | Mapping[str, Any]) -> str:
    """Deterministic plain-text operator summary (no LLM/provider calls)."""

    if isinstance(snapshot, OperatorSnapshot):
        data = snapshot.to_dict()
    else:
        data = dict(snapshot)

    def get(*path: str, default: Any = None) -> Any:
        node: Any = data
        for key in path:
            if not isinstance(node, Mapping) or key not in node:
                return default
            node = node[key]
        return node

    def fmt_list(values: Any) -> str:
        if not values:
            return "-"
        return ",".join(str(v) for v in values)

    lines: list[str] = []
    lines.append("HERMES OPERATOR SNAPSHOT")
    lines.append(f"schema_version: {get('schema_version')}")
    health = get("projection_health", "health")
    status = get("projection_health", "status")
    reason_codes = get("projection_health", "reason_codes")
    lines.append(f"health: {health} (projection: {status})")
    if reason_codes:
        lines.append(f"reasons: {fmt_list(reason_codes)}")

    cycle_id = get("cycle", "cycle_id")
    lines.append(f"cycle: {cycle_id if cycle_id else '-'}")
    lines.append(f"  task/node: {get('cycle', 'task_id')}/{get('cycle', 'node_id')}")
    lines.append(f"  intent: {get('cycle', 'intent_digest') or '-'} rev={get('cycle', 'intent_revision')}")
    lines.append(f"  repository: {get('cycle', 'repository_id')} base={get('cycle', 'source_base_sha')}")
    lines.append(f"  epoch: {get('cycle', 'execution_epoch')}")

    control = get("control_plane", default={}) or {}
    lines.append(f"phase: {control.get('phase', '-')}")
    lines.append(
        "  blocked={0} terminal={1} requalification_required={2} handoff_ready={3}".format(
            control.get("blocked"),
            control.get("terminal"),
            control.get("requalification_required"),
            control.get("handoff_ready"),
        )
    )

    workspaces = get("workspaces", default=[]) or []
    active_ws = [w for w in workspaces if (w.get("lease_state") in (None, "ACTIVE", "RESERVED", "RELEASE_PENDING"))]
    lines.append(f"workspaces: {len(workspaces)} total, {len(active_ws)} active")
    for workspace in active_ws:
        lines.append(f"  ws={workspace.get('workspace_id')} mode={workspace.get('execution_mode')} lease={workspace.get('lease_state')}")

    runs = get("runs", default=[]) or []
    active_runs = [r for r in runs if r.get("operator_state") in ("ACTIVE", "CANCEL_REQUESTED", "UNVERIFIABLE")]
    lines.append(f"runs: {len(runs)} total, {len(active_runs)} active")
    for run in active_runs:
        lines.append(f"  run={run.get('run_id')} state={run.get('operator_state')} host={run.get('execution_host_id')}")

    candidates = get("candidates", default=[]) or []
    active_candidates = [c for c in candidates if c.get("completion_state") in ("CREATED", "WORKSPACE_READY", "RUNNING", "VALIDATING")]
    lines.append(f"candidates: {len(candidates)} total, {len(active_candidates)} active")
    selected = get("control_plane", "selected_candidate_id")
    lines.append(f"selected candidate: {selected if selected else '-'}")

    judgement = get("judgement", default={}) or {}
    lines.append(f"judgement: {judgement.get('decision_state') or 'NOT_PRESENT'}")

    validation = get("validation", default={}) or {}
    lines.append(f"validation: {validation.get('status') or 'NOT_PRESENT'} freshness={validation.get('freshness')}")

    requal = get("requalification", default={}) or {}
    lines.append(f"requalification_required: {requal.get('requalification_required')}")

    remote_unverifiable = [
        h.get("execution_host_id")
        for h in (get("execution_hosts", default=[]) or [])
        if h.get("remote_state") == "UNVERIFIABLE"
    ]
    lines.append(f"remote unverifiable hosts: {fmt_list(remote_unverifiable)}")

    handoff = get("handoff", default={}) or {}
    lines.append(f"handoff: present={handoff.get('present')} ready={handoff.get('readiness')}")
    if handoff.get("missing_requirements"):
        lines.append(f"  missing: {fmt_list(handoff.get('missing_requirements'))}")

    barriers = get("barriers", default=[]) or []
    for barrier in barriers:
        marker = "READY" if barrier.get("ready") else "NOT_READY"
        lines.append(f"barrier {barrier.get('barrier_name')}: {marker}")
        if not barrier.get("ready"):
            lines.append(f"  reasons: {fmt_list(barrier.get('reason_codes'))}")

    production = get("production_serialization")
    if production:
        lines.append(
            "production serialization: ready={0} active_mutation_agents={1} owner={2}".format(
                production.get("ready"),
                production.get("active_mutation_agents"),
                production.get("production_owner") or "-",
            )
        )

    blockers = get("blockers", default=[]) or []
    lines.append(f"blockers: {len(blockers)}")
    for blocker in blockers[:10]:
        lines.append(f"  [{blocker.get('scope')}] {blocker.get('code')} ({blocker.get('affected_identity')})")

    truncations = get("truncations", default=[]) or []
    for truncation in truncations:
        if truncation.get("truncated"):
            lines.append(
                "truncated: {0} original={1} returned={2}".format(
                    truncation.get("field"),
                    truncation.get("original_count"),
                    truncation.get("returned_count"),
                )
            )

    redactions = get("redactions", default=[]) or []
    if redactions:
        lines.append(f"redactions: {len(redactions)}")

    provenance = get("generated_from", default={}) or {}
    if provenance.get("sources_absent"):
        lines.append(f"sources absent: {fmt_list(provenance.get('sources_absent'))}")

    return "\n".join(lines)
