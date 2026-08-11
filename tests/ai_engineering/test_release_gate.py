from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from ai_engineering.contracts import Status
from ai_engineering.release_gate import (
    RELEASE_GATE_POLICY_VERSION,
    RELEASE_GATE_SCHEMA_VERSION,
    BlockerScope,
    GateEvidence,
    GateName,
    ReleaseGateError,
    ReleaseTarget,
    ReleaseTaskClassification,
    SourceIdentity,
    TechnicalBlocker,
    derive_gate_requirements,
    evaluate_release,
    evaluate_release_mapping,
    normalize_release_receipt,
    release_receipt_digest,
    serialize_release_receipt,
)
from scripts.check_agent_release_gate import run as run_cli


BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40


def _classification(
    **overrides: object,
) -> ReleaseTaskClassification:
    values: dict[str, object] = {
        "task_classification": "CONSERVATIVE_PR_MERGE",
        "behaviour_sensitive": True,
        "security_sensitive": True,
        "cost_sensitive": False,
        "production_sensitive": False,
        "live_behaviour_required": False,
    }
    values.update(overrides)
    return ReleaseTaskClassification(**values)  # type: ignore[arg-type]


def _source(**overrides: str) -> SourceIdentity:
    values = {
        "repository": "life2boat/hermes",
        "canonical_remote": "github",
        "base_sha": BASE_SHA,
        "candidate_sha": HEAD_SHA,
        "observed_head_sha": HEAD_SHA,
        "task_id": "pr-release-gate-test",
    }
    values.update(overrides)
    return SourceIdentity(**values)


def _gates(
    target: ReleaseTarget = ReleaseTarget.MERGE,
    classification: ReleaseTaskClassification | None = None,
    **statuses: Status,
) -> tuple[GateEvidence, ...]:
    task = classification or _classification()
    requirements = derive_gate_requirements(target, task)
    return tuple(
        GateEvidence(
            gate_name=name,
            required=requirements[name],
            status=statuses.get(name.value, Status.PASS if requirements[name] else Status.NOT_PERFORMED),
            evidence_refs=(f"evidence:{name.value.lower()}",) if requirements[name] else (),
            reason_codes=(),
        )
        for name in GateName
    )


def _receipt(
    *,
    target: ReleaseTarget = ReleaseTarget.MERGE,
    classification: ReleaseTaskClassification | None = None,
    source: SourceIdentity | None = None,
    blockers: tuple[TechnicalBlocker, ...] = (),
    observations: tuple[str, ...] = (),
    **statuses: Status,
):
    task = classification or _classification()
    return evaluate_release(
        target=target,
        source=source or _source(),
        classification=task,
        gate_results=_gates(target, task, **statuses),
        technical_blockers=blockers,
        governance_observations=observations,
    )


def _request(
    *,
    target: ReleaseTarget = ReleaseTarget.MERGE,
    classification: ReleaseTaskClassification | None = None,
    source: SourceIdentity | None = None,
    **statuses: Status,
) -> dict[str, object]:
    task = classification or _classification()
    identity = source or _source()
    return {
        "schema_version": RELEASE_GATE_SCHEMA_VERSION,
        "policy_version": RELEASE_GATE_POLICY_VERSION,
        "target": target.value,
        "source_identity": {
            "repository": identity.repository,
            "canonical_remote": identity.canonical_remote,
            "base_sha": identity.base_sha,
            "candidate_sha": identity.candidate_sha,
            "observed_head_sha": identity.observed_head_sha,
            "task_id": identity.task_id,
        },
        "task_classification": {
            "task_classification": task.task_classification,
            "behaviour_sensitive": task.behaviour_sensitive,
            "security_sensitive": task.security_sensitive,
            "cost_sensitive": task.cost_sensitive,
            "production_sensitive": task.production_sensitive,
            "live_behaviour_required": task.live_behaviour_required,
        },
        "gate_results": [
            {
                "gate_name": gate.gate_name.value,
                "required": gate.required,
                "status": gate.status.value,
                "evidence_refs": list(gate.evidence_refs),
                "reason_codes": list(gate.reason_codes),
                "evidence_digest": gate.evidence_digest,
            }
            for gate in _gates(target, task, **statuses)
        ],
        "technical_blockers": [],
        "governance_observations": [],
    }


def test_all_required_merge_gates_pass_without_production_claim() -> None:
    receipt = _receipt()
    assert receipt.merge_eligible is Status.PASS
    assert receipt.production_release_eligible is Status.NOT_PERFORMED
    assert receipt.status is Status.PASS
    assert receipt.required_gates == (
        GateName.CODE,
        GateName.BEHAVIOUR,
        GateName.SECURITY,
    )


@pytest.mark.parametrize(
    "gate_name",
    [GateName.CODE, GateName.BEHAVIOUR, GateName.SECURITY],
)
def test_each_required_merge_gate_failure_fails(gate_name: GateName) -> None:
    receipt = _receipt(**{gate_name.value: Status.FAIL})
    assert receipt.merge_eligible is Status.FAIL


@pytest.mark.parametrize(
    "status",
    [
        Status.BLOCKED,
        Status.UNKNOWN,
        Status.NOT_RUN,
        Status.NOT_PERFORMED,
        Status.INCONCLUSIVE,
    ],
)
def test_unresolved_required_gate_blocks_and_never_passes(status: Status) -> None:
    receipt = _receipt(**{GateName.BEHAVIOUR.value: status})
    assert receipt.merge_eligible is Status.BLOCKED
    assert receipt.status is Status.BLOCKED


def test_optional_gates_remain_visible_without_blocking_merge() -> None:
    receipt = _receipt()
    by_name = {item.gate_name: item for item in receipt.gate_results}
    for name in (
        GateName.LIVE_BEHAVIOUR,
        GateName.COST,
        GateName.PRODUCTION_READINESS,
    ):
        assert by_name[name].required is False
        assert by_name[name].status is Status.NOT_PERFORMED
    assert receipt.merge_eligible is Status.PASS


def test_production_release_requires_separate_live_cost_and_readiness_pass() -> None:
    task = _classification(
        cost_sensitive=True,
        production_sensitive=True,
        live_behaviour_required=True,
    )
    receipt = _receipt(target=ReleaseTarget.PRODUCTION_RELEASE, classification=task)
    assert receipt.merge_eligible is Status.PASS
    assert receipt.production_release_eligible is Status.PASS
    assert receipt.status is Status.PASS


def test_required_unknown_cost_blocks_production_even_with_model_policy_pass() -> None:
    task = _classification(cost_sensitive=True, production_sensitive=True)
    receipt = _receipt(
        target=ReleaseTarget.PRODUCTION_RELEASE,
        classification=task,
        **{GateName.COST.value: Status.UNKNOWN},
    )
    assert receipt.merge_eligible is Status.PASS
    assert receipt.production_release_eligible is Status.BLOCKED


def test_not_performed_production_readiness_blocks_release() -> None:
    task = _classification(production_sensitive=True)
    receipt = _receipt(
        target=ReleaseTarget.PRODUCTION_RELEASE,
        classification=task,
        **{GateName.PRODUCTION_READINESS.value: Status.NOT_PERFORMED},
    )
    assert receipt.production_release_eligible is Status.BLOCKED


def test_merge_failure_prevents_production_pass() -> None:
    task = _classification(production_sensitive=True)
    receipt = _receipt(
        target=ReleaseTarget.PRODUCTION_RELEASE,
        classification=task,
        **{GateName.SECURITY.value: Status.FAIL},
    )
    assert receipt.merge_eligible is Status.FAIL
    assert receipt.production_release_eligible is Status.FAIL


def test_requirement_derivation_cannot_be_weakened_by_caller() -> None:
    gates = list(_gates())
    index = next(i for i, item in enumerate(gates) if item.gate_name is GateName.SECURITY)
    gates[index] = replace(gates[index], required=False)
    with pytest.raises(ReleaseGateError) as caught:
        evaluate_release(
            target=ReleaseTarget.MERGE,
            source=_source(),
            classification=_classification(),
            gate_results=gates,
        )
    assert caught.value.code == "RELEASE_GATE_REQUIREMENT_MISMATCH"

def test_required_gate_cannot_pass_without_explicit_evidence() -> None:
    gates = list(_gates())
    index = next(
        i for i, item in enumerate(gates) if item.gate_name is GateName.BEHAVIOUR
    )
    gates[index] = replace(gates[index], evidence_refs=())
    with pytest.raises(ReleaseGateError) as caught:
        evaluate_release(
            target=ReleaseTarget.MERGE,
            source=_source(),
            classification=_classification(),
            gate_results=gates,
        )
    assert caught.value.code == "RELEASE_GATE_REQUIRED_EVIDENCE_MISSING"


def test_candidate_head_mismatch_is_a_technical_failure() -> None:
    receipt = _receipt(source=_source(observed_head_sha="3" * 40))
    assert receipt.merge_eligible is Status.FAIL
    assert receipt.technical_blockers[0].code == "EXACT_SHA_MISMATCH"


@pytest.mark.parametrize("bad_sha", ["", "abc", "G" * 40, "1" * 39])
def test_missing_or_malformed_source_identity_blocks(bad_sha: str) -> None:
    request = _request()
    request["source_identity"]["candidate_sha"] = bad_sha  # type: ignore[index]
    with pytest.raises(ReleaseGateError) as caught:
        evaluate_release_mapping(request)
    assert caught.value.code == "RELEASE_GATE_SOURCE_IDENTITY_MISSING"


def test_unknown_target_and_gate_block_validation() -> None:
    target = _request()
    target["target"] = "DEPLOY"
    with pytest.raises(ReleaseGateError) as caught:
        evaluate_release_mapping(target)
    assert caught.value.code == "RELEASE_GATE_TARGET_UNKNOWN"

    gate = _request()
    gate["gate_results"][0]["gate_name"] = "MODEL_POLICY_GATE"  # type: ignore[index]
    with pytest.raises(ReleaseGateError) as caught:
        evaluate_release_mapping(gate)
    assert caught.value.code == "RELEASE_GATE_GATE_UNKNOWN"


def test_technical_blockers_are_separate_and_have_target_scope() -> None:
    merge_blocker = TechnicalBlocker(
        code="EXACT_SHA_MISMATCH",
        status=Status.FAIL,
        evidence_refs=("source:head",),
        scope=BlockerScope.MERGE,
    )
    assert _receipt(blockers=(merge_blocker,)).merge_eligible is Status.FAIL

    task = _classification(production_sensitive=True)
    production_blocker = TechnicalBlocker(
        code="ROLLBACK_EVIDENCE_MISSING",
        status=Status.BLOCKED,
        evidence_refs=("production:rollback",),
        scope=BlockerScope.PRODUCTION_RELEASE,
    )
    receipt = _receipt(
        target=ReleaseTarget.PRODUCTION_RELEASE,
        classification=task,
        blockers=(production_blocker,),
    )
    assert receipt.merge_eligible is Status.PASS
    assert receipt.production_release_eligible is Status.BLOCKED


def test_governance_observations_are_visible_ordered_and_nonblocking() -> None:
    receipt = _receipt(
        observations=(
            "OPTIONAL_INDEPENDENT_REVIEW_ABSENT",
            "BRANCH_PROTECTION_ENABLED_FALSE",
        )
    )
    assert receipt.merge_eligible is Status.PASS
    assert receipt.governance_observations == (
        "BRANCH_PROTECTION_ENABLED_FALSE",
        "OPTIONAL_INDEPENDENT_REVIEW_ABSENT",
    )
    assert receipt.technical_blockers == ()


def test_canonical_serialization_is_order_independent_and_meaning_sensitive() -> None:
    first = _receipt(
        observations=("OPTIONAL_AUDIT_ABSENT", "BRANCH_PROTECTION_ENABLED_FALSE")
    )
    second = _receipt(
        observations=("BRANCH_PROTECTION_ENABLED_FALSE", "OPTIONAL_AUDIT_ABSENT")
    )
    assert serialize_release_receipt(first) == serialize_release_receipt(second)
    assert release_receipt_digest(first) == release_receipt_digest(second)
    changed = _receipt(**{GateName.SECURITY.value: Status.FAIL})
    assert release_receipt_digest(first) != release_receipt_digest(changed)
    assert json.loads(serialize_release_receipt(first)) == normalize_release_receipt(first)


@pytest.mark.parametrize(
    "forbidden",
    [
        "raw_prompt",
        "chain_of_thought",
        "raw_user_message",
        "raw_provider_response",
        "credential",
        "api_key",
        "password",
        "secret",
        "environment",
        "raw_production_log",
    ],
)
def test_forbidden_private_fields_are_rejected_without_echo(forbidden: str) -> None:
    request = _request()
    request[forbidden] = "synthetic-private-value"
    with pytest.raises(ReleaseGateError) as caught:
        evaluate_release_mapping(request)
    assert caught.value.code == "RELEASE_GATE_EVIDENCE_UNSAFE"
    assert "synthetic-private-value" not in str(caught.value)


def _write_request(path: Path, request: dict[str, object]) -> None:
    path.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")


def test_cli_exit_codes_for_pass_fail_blocked_and_malformed(
    tmp_path: Path, capsys
) -> None:
    cases = (
        (_request(), 0, "PASS"),
        (_request(**{GateName.SECURITY.value: Status.FAIL}), 1, "FAIL"),
        (_request(**{GateName.BEHAVIOUR.value: Status.UNKNOWN}), 2, "BLOCKED"),
    )
    for index, (request, expected_exit, expected_status) in enumerate(cases):
        path = tmp_path / f"case-{index}.json"
        _write_request(path, request)
        assert run_cli(["evaluate", "--root", str(tmp_path), "--input", path.name]) == expected_exit
        payload = json.loads(capsys.readouterr().out.splitlines()[0])
        assert payload["status"] == expected_status

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    assert run_cli(["evaluate", "--root", str(tmp_path), "--input", malformed.name]) == 2
    payload = json.loads(capsys.readouterr().out.splitlines()[0])
    assert payload["status"] == "BLOCKED"


def test_cli_production_target_uses_production_decision(tmp_path: Path, capsys) -> None:
    task = _classification(cost_sensitive=True, production_sensitive=True)
    request = _request(
        target=ReleaseTarget.PRODUCTION_RELEASE,
        classification=task,
        **{GateName.PRODUCTION_READINESS.value: Status.NOT_PERFORMED},
    )
    path = tmp_path / "production.json"
    _write_request(path, request)
    assert run_cli(["evaluate", "--root", str(tmp_path), "--input", path.name]) == 2
    payload = json.loads(capsys.readouterr().out.splitlines()[0])
    assert payload["merge_eligible"] == "PASS"
    assert payload["production_release_eligible"] == "BLOCKED"


def test_agent_release_workflow_is_exact_head_read_only_and_pinned() -> None:
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/agent-release-gate.yml").read_text(
        encoding="utf-8"
    )
    assert "permissions:\n  contents: read" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "fetch-depth: 0" in workflow
    assert "persist-credentials: false" in workflow
    assert "check_agent_release_gate.py ci-merge" in workflow
    assert "deploy" not in "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    ).casefold()
    assert "contents: write" not in workflow
    assert "pull-requests: write" not in workflow
    assert "packages: write" not in workflow
    uses = re.findall(r"uses:\s*([^\s#]+)", workflow)
    assert uses
    assert all(re.search(r"@[0-9a-f]{40}$", item) for item in uses)


def test_agent_check_does_not_recursively_invoke_release_gate() -> None:
    root = Path(__file__).resolve().parents[2]
    agent_check = (root / "scripts/agent_check.sh").read_text(encoding="utf-8")
    assert "check_agent_release_gate" not in agent_check


def test_ci_merge_binds_secret_scan_to_exact_candidate_range() -> None:
    root = Path(__file__).resolve().parents[2]
    cli = (root / "scripts/check_agent_release_gate.py").read_text(
        encoding="utf-8"
    )
    secret_check = (root / "scripts/secret_check.sh").read_text(
        encoding="utf-8"
    )
    for name in (
        "HERMES_SECRET_CHECK_BASE_SHA",
        "HERMES_SECRET_CHECK_SOURCE_SHA",
    ):
        assert name in cli
        assert name in secret_check
    assert "candidate_tree_entries" in secret_check
