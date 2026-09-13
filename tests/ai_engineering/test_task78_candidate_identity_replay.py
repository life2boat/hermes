import os
import uuid
import json
import pytest
import datetime
import hashlib
from pathlib import Path

from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.task_intent import TaskIntent, intent_digest, IntentStatus
from ai_engineering.supervisor.events import create_event, SupervisorEventType
from ai_engineering.contracts import StopBoundary, TaskClass, EffectClass
from ai_engineering.supervisor.pr_provider import compute_candidate_head_identity_digest, CANDIDATE_HEAD_IDENTITY_SCHEMA_VERSION, CandidateHeadIdentity
from ai_engineering.supervisor.autonomous_run import AutonomousRunCoordinator, ScriptedAstraProposalProvider, BudgetConfig, NextActionType, AstraNextActionProposal, FakeGitHubPullRequestBackend

def _make_intent():
    return TaskIntent(
        task_id="task-123",
        schema_version=1,
        source_repository="life2boat/hermes",
        source_base_sha="4da55cbf3722f7964dd10d56725b45a5456f8c02",
        source_main_ref="main",
        desired_outcome="Test outcome",
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        stop_boundary=StopBoundary.DRAFT_PR,
        allowed_mutations=(),
        forbidden_mutations=(),
        required_gates=(),
        parent_intent_digest="0" * 64,
        status=IntentStatus.READY,
        constraints=(),
        acceptance_criteria=(),
        unknowns=(),
        applicable_invariants=(),
        intent_revision=1
    )

def _create_work_profile():
    from ai_engineering.supervisor.policy.contracts import AutonomyLevel, EffectClass
    from ai_engineering.supervisor.policy.work_profile import validate_work_profile
    raw = {
        "schema_version": "hermes.work-profile.v1",
        "profile_id": "test-wp",
        "profile_version": 1,
        "preferred_worker_model": None,
        "preferred_verifier_model": None,
        "preferred_supervisor_model": None,
        "escalation_model": None,
        "allowed_task_classes": ["BOUNDED_IMPLEMENTATION"],
        "maximum_autonomy_level": AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION.value,
        "allowed_effect_classes": [EffectClass.READ_ONLY.value, EffectClass.REPOSITORY_WRITE.value, EffectClass.PR_MUTATION.value, EffectClass.PR_MERGE.value],
        "forbidden_effect_classes": [],
        "allowed_targets": ["DEV", "LOCAL"],
        "required_validators": [],
        "promotion_thresholds": {
            "required_successful_runs": 1,
            "allowed_critical_failures": 0,
            "require_rollback_verified": False,
        },
        "budget_limits": {
            "max_supervisor_decisions": 20,
            "max_child_tasks": 10,
            "max_retries_per_task": 3,
            "max_fix_cycles_per_task": 3,
            "max_consecutive_failures": 2,
            "max_provider_calls": 50,
            "max_policy_denials": 10,
        },
        "production_execution_allowed": False,
        "vector_mutation_allowed": False,
        "secret_mutation_allowed": False,
        "external_send_allowed": False,
    }
    return validate_work_profile(raw)

def _create_effective_policy(intent):
    from ai_engineering.effective_policy import EffectivePolicyReport, TaskPolicyAttribution, EffectivePolicyStatus
    tpa = TaskPolicyAttribution(
        task_id=intent.task_id,
        intent_revision=intent.intent_revision,
        intent_digest=intent_digest(intent),
        source_base_sha=intent.source_base_sha,
        constraints=intent.constraints,
        allowed_mutations=intent.allowed_mutations,
        forbidden_mutations=intent.forbidden_mutations,
        stop_boundary=intent.stop_boundary.value,
        source_id="0" * 64,
    )
    return EffectivePolicyReport(
        schema_version=1,
        effective_policy_id="0" * 64,
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        source_base_sha=intent.source_base_sha,
        subject_sha=intent.source_base_sha,
        status=EffectivePolicyStatus.COMPLETE,
        policy_sources=(),
        task_policy=tpa,
        invariant_resolutions=(),
        required_gate_resolutions=(),
        unresolved_references=(),
        precedence_source_id="0" * 64,
        authority_expansion=False,
    )

def _setup_run(tmp_path):
    run_id = "run-test-" + uuid.uuid4().hex[:8]
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _make_intent()
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    state = loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal="test",
        root_goal_id=run_id,
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    state = loop.bind_profile(run_id, wp, ep)
    return store, run_id, state, loop

def _valid_payload():
    payload = {
        "schema_version": CANDIDATE_HEAD_IDENTITY_SCHEMA_VERSION,
        "candidate_id": "cid-123",
        "run_id": "run-test",
        "task_id": "task-123",
        "repository": "life2boat/hermes",
        "remote_name": "github",
        "head_ref": "feat/test",
        "head_sha": "a" * 40,
        "base_ref": "main",
        "base_sha": "b" * 40,
        "source_base_sha": "c" * 40,
        "published_at_utc": "2026-09-13T00:00:00+00:00"
    }
    payload["digest"] = compute_candidate_head_identity_digest(payload)
    return payload

def _inject_cid_event(store, run_id, payload):
    events = store.load_events(run_id)
    prev_digest = events[-1].event_digest if events else None
    seq = len(events) + 1
    ev = create_event(
        run_id=run_id,
        sequence=seq,
        previous_event_digest=prev_digest,
        event_type=SupervisorEventType.CANDIDATE_HEAD_IDENTITY_REGISTERED,
        state_revision=1,
        task_id="task-123",
        attempt_id="att-1",
        intent_digest="0" * 64,
        payload=payload,
        created_at_utc="2026-09-13T00:00:00+00:00"
    )
    store.save_event(ev)

def _create_coordinator(store, run_id, provider_mode="test", pr_provider=None, candidate_head_ref=None, candidate_head_identity=None, astra_provider=None):
    from ai_engineering.supervisor.router.router import CrossAgentRouter
    from ai_engineering.supervisor.collector import ResultCollector
    loop = SupervisorLoop(store)
    class MockRouter:
        def route_result(self, r): pass
        def send_to_task(self, t, m): pass
        def terminate(self): pass
        async def dispatch(self, env):
            raise ValueError(f"DISPATCHED WITH EFFECT CLASS: {env.effect_class} STOP BOUNDARY: {env.stop_boundary}")
    router = MockRouter()
    class MockResultCollector:
        def write(self, c): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=router,
        result_collector=MockResultCollector(),
        budget=BudgetConfig(),
        run_id=run_id,
        astra_provider=astra_provider or ScriptedAstraProposalProvider([]),
        ci_provider=None,
        evidence_root="/tmp",
        provider_mode=provider_mode,
        pr_provider=pr_provider,
        allow_pr_create=False,
        allow_pr_merge=False,
        candidate_head_ref=candidate_head_ref,
        candidate_head_identity=candidate_head_identity,
    )

@pytest.mark.asyncio
async def test_candidate_identity_replay_missing_digest(tmp_path):
    store, run_id, state, loop = _setup_run(tmp_path)
    payload = _valid_payload()
    del payload["digest"]
    _inject_cid_event(store, run_id, payload)

    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()

    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"
    assert coordinator._candidate_head_identity_blocked is True

    events = store.load_events(run_id)
    assert any(e.event_type == SupervisorEventType.CANDIDATE_HEAD_IDENTITY_INVALID for e in events)

@pytest.mark.asyncio
async def test_candidate_identity_replay_empty_head_sha(tmp_path):
    store, run_id, state, loop = _setup_run(tmp_path)
    payload = _valid_payload()
    payload["head_sha"] = ""
    payload["digest"] = compute_candidate_head_identity_digest(payload)
    _inject_cid_event(store, run_id, payload)

    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()

    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"

@pytest.mark.asyncio
async def test_candidate_identity_replay_missing_repo(tmp_path):
    store, run_id, state, loop = _setup_run(tmp_path)
    payload = _valid_payload()
    del payload["repository"]
    # Intentionally do not recompute digest to trigger schema failure before digest failure
    _inject_cid_event(store, run_id, payload)

    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()

    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"

@pytest.mark.asyncio
async def test_candidate_identity_replay_wrong_schema(tmp_path):
    store, run_id, state, loop = _setup_run(tmp_path)
    payload = _valid_payload()
    payload["schema_version"] = "hermes.candidate-head-identity.v999"
    payload["digest"] = compute_candidate_head_identity_digest(payload)
    _inject_cid_event(store, run_id, payload)

    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()

    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"

@pytest.mark.asyncio
async def test_candidate_identity_replay_malformed_payload(tmp_path):
    store, run_id, state, loop = _setup_run(tmp_path)
    _inject_cid_event(store, run_id, {"foo": "bar"})

    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()

    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"

@pytest.mark.asyncio
async def test_candidate_identity_replay_digest_mismatch(tmp_path):
    store, run_id, state, loop = _setup_run(tmp_path)
    payload = _valid_payload()
    payload["digest"] = "bad" * 21 + "b"
    _inject_cid_event(store, run_id, payload)

    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()

    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"
    assert coordinator._candidate_head_identity_blocked is True

@pytest.mark.asyncio
async def test_candidate_identity_replay_corrupt_with_cli_ref(tmp_path):
    store, run_id, state, loop = _setup_run(tmp_path)
    payload = _valid_payload()
    payload["digest"] = "bad" * 21 + "b"
    _inject_cid_event(store, run_id, payload)

    coordinator = _create_coordinator(store, run_id, candidate_head_ref="valid/ref")
    receipt = await coordinator.run_until_terminal()

    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"

@pytest.mark.asyncio
async def test_candidate_identity_injected_real_mode(tmp_path):
    store, run_id, state, loop = _setup_run(tmp_path)

    from ai_engineering.supervisor.validator import VerifiedResult
    from ai_engineering.contracts import Status
    vr = VerifiedResult(
        schema_version="hermes.verified-result.v1",
        result_id="vr-123",
        task_id=state.current_task_id,
        attempt_id=state.current_attempt_id,
        intent_digest=state.current_intent_digest,
        base_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        normalized_evidence_digest="mock-evidence",
        status=Status.PASS,
        head_sha="8888888888888888888888888888888888888888",
        gate_results=(),
        required_gates=(),
        blockers=(),
        reason_codes=(),
        verified_at_utc="2026-09-13T00:00:00+00:00"
    )
    from ai_engineering.supervisor.validator import compute_result_id
    state = loop.ingest_verified_result(run_id, vr, "vr-123")

    cid_payload = _valid_payload()
    cid = CandidateHeadIdentity(**cid_payload)

    astra = ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR, NextActionType.STOP_SUCCESS])

    coordinator = _create_coordinator(store, run_id, provider_mode="real", candidate_head_identity=cid, candidate_head_ref=None, astra_provider=astra)
    coordinator.pr_provider = MyRealMockPRBackend()

    class MockCI:
        def get_check_statuses(self, *a, **kw): return {}
    coordinator.ci_provider = MockCI()
    coordinator.allow_pr_create = True

    from ai_engineering.supervisor.decision import DecisionReceipt

    class MockLoop:
        def __init__(self, loop, st):
            self.loop = loop
            self.st = st
        def accept_decision(self, *a, **kw):

            from ai_engineering.supervisor.decision import DecisionReceipt
            return DecisionReceipt(
                schema_version="hermes.decision-receipt.v1",
                run_id="run-test",
                task_id="task-123",
                attempt_id="att-1",
                context_pack_digest="123",
                verified_result_id="vr-123",
                verified_result_digest="123",
                decision_id="dec-123",
                receipt_id="rec-123",
                decision_digest="123",
                intent_digest="123",
                action=NextActionType.CREATE_PR.value,
                validated_at_utc="2026-09-13T00:00:00Z"
            ), self.st
        def __getattr__(self, item):
            return getattr(self.loop, item)

    coordinator.loop = MockLoop(loop, state)

    receipt = await coordinator.run_until_terminal()
    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_MISSING"


class MyRealMockPRBackend:
    def __init__(self):
        self.auto_seed_remote_head = False

    async def get_remote_head_sha(self, repo, head_branch):
        return "8888888888888888888888888888888888888888"

    async def find_existing_pr(self, repo, head_branch, base_branch):
        return None




    async def create_pr(self, *args, **kwargs):
        from ai_engineering.supervisor.pr_provider import PullRequestIdentity
        return PullRequestIdentity(
            schema_version="hermes.pull-request-identity.v1",
            repository=kwargs.get("repository", "test/repo"),
            pr_number=999,
            pr_node_id="node-123",
            head_branch=kwargs.get("head_branch", "feat"),
            head_sha=kwargs.get("head_sha", "8888888888888888888888888888888888888888"),
            base_branch=kwargs.get("base_branch", "main"),
            base_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            title="title",
            body="body",
            is_draft=kwargs.get("draft", False),
            state="open",
            mergeable=True,
            mergeable_state="clean",
            merged=False,
            merged_at=None,
            merge_commit_sha=None,
            created_at_utc="2026-09-13T00:00:00Z",
            updated_at_utc="2026-09-13T00:00:00Z"
        )




    async def merge_pr(self, repo, pr_number, merge_method):
        return "9999999999999999999999999999999999999999"


@pytest.mark.asyncio
async def test_candidate_identity_explicit_ref_real_mode(tmp_path):
    store, run_id, state, loop = _setup_run(tmp_path)

    from ai_engineering.supervisor.validator import VerifiedResult
    from ai_engineering.contracts import Status
    vr = VerifiedResult(
        schema_version="hermes.verified-result.v1",
        result_id="vr-123",
        task_id=state.current_task_id,
        attempt_id=state.current_attempt_id,
        intent_digest=state.current_intent_digest,
        base_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        normalized_evidence_digest="mock-evidence",
        status=Status.PASS,
        head_sha="8888888888888888888888888888888888888888",
        gate_results=(),
        required_gates=(),
        blockers=(),
        reason_codes=(),
        verified_at_utc="2026-09-13T00:00:00+00:00"
    )
    from ai_engineering.supervisor.validator import compute_result_id
    state = loop.ingest_verified_result(run_id, vr, "vr-123")

    cid_payload = _valid_payload()
    cid_payload["head_ref"] = "feat/forged"
    cid_payload["head_sha"] = "0000000000000000000000000000000000000000"
    cid = CandidateHeadIdentity(**cid_payload)

    astra = ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR, NextActionType.STOP_SUCCESS])

    coordinator = _create_coordinator(store, run_id, provider_mode="real", candidate_head_identity=cid, candidate_head_ref="trusted-explicit-ref", astra_provider=astra)
    coordinator.pr_provider = MyRealMockPRBackend()

    class MockCI:
        def get_check_statuses(self, *a, **kw): return {"agent-release-gate": "success"}
    coordinator.ci_provider = MockCI()
    coordinator.allow_pr_create = True

    from ai_engineering.supervisor.decision import DecisionReceipt

    class MockLoop:
        def __init__(self, loop, st):
            self.loop = loop
            self.st = st
        def accept_decision(self, *a, **kw):

            from ai_engineering.supervisor.decision import DecisionReceipt
            return DecisionReceipt(
                schema_version="hermes.decision-receipt.v1",
                run_id="run-test",
                task_id="task-123",
                attempt_id="att-1",
                context_pack_digest="123",
                verified_result_id="vr-123",
                verified_result_digest="123",
                decision_id="dec-123",
                receipt_id="rec-123",
                decision_digest="123",
                intent_digest="123",
                action=NextActionType.CREATE_PR.value,
                validated_at_utc="2026-09-13T00:00:00Z"
            ), self.st
        def __getattr__(self, item):
            return getattr(self.loop, item)

    coordinator.loop = MockLoop(loop, state)

    receipt = await coordinator.run_until_terminal()
    assert coordinator.active_pr is not None; assert coordinator.candidate_head_identity.head_ref == "trusted-explicit-ref"

    new_cid = getattr(coordinator, "candidate_head_identity", None)
    assert new_cid is not None
    assert new_cid.head_ref == "trusted-explicit-ref"
    assert new_cid.head_sha == "8888888888888888888888888888888888888888"


@pytest.mark.asyncio
async def test_candidate_identity_valid_persisted(tmp_path):
    store, run_id, state, loop = _setup_run(tmp_path)
    payload = _valid_payload()
    _inject_cid_event(store, run_id, payload)

    coordinator = _create_coordinator(store, run_id, provider_mode="real")
    await coordinator.run_until_terminal()

    assert getattr(coordinator, "candidate_head_identity", None) is not None
    assert getattr(coordinator, "candidate_head_identity").digest == payload["digest"]

@pytest.mark.asyncio
async def test_candidate_identity_valid_cli_ref(tmp_path):
    store, run_id, state, loop = _setup_run(tmp_path)
    coordinator = _create_coordinator(store, run_id, candidate_head_ref="feat/some-branch")

    assert getattr(coordinator, "candidate_head_identity", None) is None
    assert getattr(coordinator, "candidate_head_ref", None) == "feat/some-branch"
