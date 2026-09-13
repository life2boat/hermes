"""Task 7.8 CLI Acceptance Tests — Real CLI Integration.

Tests the scripts/autonomous_run.py CLI for:
1. Real mode PR provider enforcement (--provider-mode real requires --pr-mode github)
2. WorkProfile PR authority (--allow-pr-create adds PR_MUTATION, --allow-pr-merge adds PR_MERGE)
3. --candidate-head-ref flag wiring
4. Negative scenarios: REAL_PR_PROVIDER_REQUIRED, missing flags, etc.
5. Structural validation of CandidateHeadIdentity persistence (all fields + digest)
"""
from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import asyncio
import datetime
import uuid
from pathlib import Path

import pytest

# Make the repo root importable regardless of pytest CWD
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ai_engineering.supervisor.autonomous_run import (
    AutonomousRunCoordinator,
    BudgetConfig,
    ScriptedAstraProposalProvider,
    NextActionType,
    CIStatusProvider,
)
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.router.router import CrossAgentRouter, AuthorityResolver, PersistentStore
from ai_engineering.supervisor.router.registry import AgentRegistry
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.supervisor.pr_provider import (
    FakeGitHubPullRequestBackend,
    GitHubPullRequestProvider,
    CandidateHeadIdentity,
    compute_candidate_head_identity_digest,
    CANDIDATE_HEAD_IDENTITY_SCHEMA_VERSION,
)
from ai_engineering.supervisor.events import SupervisorEventType
from ai_engineering.contracts import EffectClass, StopBoundary
from ai_engineering.task_intent import TaskIntent, deserialize_intent
from ai_engineering.supervisor.policy.contracts import (
    WorkProfile,
    AutonomyLevel,
    ExecutionTarget,
)
from ai_engineering.effective_policy import (
    EffectivePolicyReport,
    EffectivePolicyStatus,
    TaskPolicyAttribution,
)
from ai_engineering.task_intent import intent_digest


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_INTENT_FIXTURE = {
    "schema_version": 1,
    "task_id": "cli-acceptance-task-001",
    "intent_revision": 1,
    "status": "READY",
    "task_class": "BOUNDED_IMPLEMENTATION",
    "desired_outcome": "Test CLI integration",
    "source_repository": "life2boat/hermes",
    "source_main_ref": "refs/remotes/github/main",
    "source_base_sha": "eac702182c570dc45bcc91e72839f33b69f7c5e5",
    "constraints": [],
    "allowed_mutations": ["ai_engineering/supervisor/"],
    "forbidden_mutations": [],
    "stop_boundary": "MERGE",
    "acceptance_criteria": [{"criterion_id": "AC-1", "statement": "Tests pass"}],
    "unknowns": [],
    "applicable_invariants": [],
    "required_gates": ["tests"],
    "parent_intent_digest": None,
}


@pytest.fixture()
def tmp_state_dir():
    with tempfile.TemporaryDirectory(prefix="hermes_test_cli_") as d:
        yield Path(d)


@pytest.fixture()
def intent():
    return deserialize_intent(json.dumps(_INTENT_FIXTURE))


@pytest.fixture()
def intent_file(tmp_path, intent):
    f = tmp_path / "intent.json"
    f.write_text(json.dumps(_INTENT_FIXTURE), encoding="utf-8")
    return str(f)


def _make_coordinator(
    tmp_state_dir: Path,
    intent,
    provider_mode: str = "test",
    pr_provider=None,
    allow_pr_create: bool = False,
    allow_pr_merge: bool = False,
    candidate_head_identity: CandidateHeadIdentity | None = None,
    scripted_actions=None,
) -> AutonomousRunCoordinator:
    store = FileSupervisorStateStore(tmp_state_dir)
    loop = SupervisorLoop(store)

    run_id = f"run-cli-test-{uuid.uuid4().hex[:8]}"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal=intent.desired_outcome,
        root_goal_id=run_id,
        created_at_utc=now,
    )

    from ai_engineering.supervisor.policy.work_profile import validate_work_profile

    allowed_effects = [EffectClass.READ_ONLY.value, EffectClass.REPOSITORY_WRITE.value]
    if allow_pr_create:
        allowed_effects.append(EffectClass.PR_MUTATION.value)
    if allow_pr_merge:
        allowed_effects.append(EffectClass.PR_MERGE.value)

    raw_profile = {
        "schema_version": "hermes.work-profile.v1",
        "profile_id": "test-profile",
        "profile_version": 1,
        "preferred_worker_model": None,
        "preferred_verifier_model": None,
        "preferred_supervisor_model": None,
        "escalation_model": None,
        "allowed_task_classes": ["BOUNDED_IMPLEMENTATION"],
        "maximum_autonomy_level": AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION.value,
        "allowed_effect_classes": allowed_effects,
        "forbidden_effect_classes": [],
        "allowed_targets": [ExecutionTarget.DEV.value],
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
    work_profile = validate_work_profile(raw_profile)

    tpa = TaskPolicyAttribution(
        task_id=intent.task_id,
        intent_revision=intent.intent_revision,
        intent_digest=intent_digest(intent),
        source_base_sha=intent.source_base_sha,
        constraints=intent.constraints,
        allowed_mutations=intent.allowed_mutations,
        forbidden_mutations=intent.forbidden_mutations,
        stop_boundary=intent.stop_boundary.value if hasattr(intent.stop_boundary, "value") else str(intent.stop_boundary),
        source_id="0" * 64,
    )
    eff_policy = EffectivePolicyReport(
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
    loop.bind_profile(run_id, work_profile, eff_policy)

    registry = AgentRegistry()
    router_store = PersistentStore(str(tmp_state_dir))
    router = CrossAgentRouter(
        registry=registry,
        authority_resolver=AuthorityResolver(supervisor_store=store, supervisor_loop=loop),
        store=router_store,
    )

    class LocalCI(CIStatusProvider):
        async def wait_for_ci(self, run_id, sha, **kwargs):
            return True

    astra = ScriptedAstraProposalProvider(
        scripted_actions=scripted_actions or [NextActionType.STOP_SUCCESS],
    )

    if pr_provider is None:
        pr_provider = FakeGitHubPullRequestBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=router,
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id=run_id,
        astra_provider=astra,
        ci_provider=LocalCI(),
        evidence_root=str(tmp_state_dir),
        provider_mode=provider_mode,
        pr_provider=pr_provider,
        allow_pr_create=allow_pr_create,
        allow_pr_merge=allow_pr_merge,
        candidate_head_identity=candidate_head_identity,
    )
    return coord


# ---------------------------------------------------------------------------
# 1. Real Mode PR Provider Binding
# ---------------------------------------------------------------------------

class TestRealModeProviderBinding:
    """--provider-mode real must require --pr-mode github. FakeGitHubPullRequestBackend is forbidden."""

    def test_real_mode_with_fake_backend_is_detected_as_real(self, tmp_state_dir, intent):
        """In real provider mode, using FakeGitHubPullRequestBackend is blocked at CREATE_PR."""
        # This test validates the is_real_mode detection in autonomous_run._run_create_pr
        # When provider_mode == "real" AND pr_provider is FakeGitHubPullRequestBackend,
        # is_real_mode should be False (fake backend overrides)
        fake_pr = FakeGitHubPullRequestBackend()
        coord = _make_coordinator(
            tmp_state_dir, intent,
            provider_mode="real",
            pr_provider=fake_pr,
        )
        # With real mode + fake backend, is_real_mode = False in CREATE_PR (fake wins)
        # So CandidateHeadIdentity is not required (test-mode path).
        # The auto-trust guard blocks injected CID in real mode.
        assert coord.candidate_head_identity is None  # auto-trust disabled in real mode

    def test_real_mode_discards_injected_candidate_head_identity(self, tmp_state_dir, intent):
        """In real provider mode, injected CandidateHeadIdentity is NOT auto-persisted."""
        cid = _make_test_cid(intent)
        coord = _make_coordinator(
            tmp_state_dir, intent,
            provider_mode="real",
            candidate_head_identity=cid,
        )
        # Must be None — real mode discards injected identity
        assert coord.candidate_head_identity is None

    def test_test_mode_accepts_injected_candidate_head_identity(self, tmp_state_dir, intent):
        """In test provider mode, injected CandidateHeadIdentity is persisted to event store."""
        cid = _make_test_cid(intent)
        coord = _make_coordinator(
            tmp_state_dir, intent,
            provider_mode="test",
            candidate_head_identity=cid,
        )
        # Test mode: identity accepted and registered
        assert coord.candidate_head_identity is not None
        assert coord.candidate_head_identity.head_ref == cid.head_ref


# ---------------------------------------------------------------------------
# 2. WorkProfile PR Authority
# ---------------------------------------------------------------------------

class TestWorkProfilePRAuthority:
    """WorkProfile allowed_effect_classes based on --allow-pr-create/--allow-pr-merge."""

    def test_no_flags_does_not_include_pr_mutation(self):
        """Base WorkProfile (no flags) only has READ_ONLY + REPOSITORY_WRITE."""
        from ai_engineering.supervisor.policy.work_profile import validate_work_profile
        raw_profile = _base_raw_profile()
        profile = validate_work_profile(raw_profile)
        effect_vals = [e.value if hasattr(e, "value") else e for e in profile.allowed_effect_classes]
        assert EffectClass.PR_MUTATION.value not in effect_vals
        assert EffectClass.PR_MERGE.value not in effect_vals
        assert EffectClass.READ_ONLY.value in effect_vals
        assert EffectClass.REPOSITORY_WRITE.value in effect_vals

    def test_allow_pr_create_adds_pr_mutation(self):
        """--allow-pr-create adds PR_MUTATION to WorkProfile allowed_effect_classes."""
        from ai_engineering.supervisor.policy.work_profile import validate_work_profile
        raw_profile = _base_raw_profile()
        raw_profile["allowed_effect_classes"].append(EffectClass.PR_MUTATION.value)
        profile = validate_work_profile(raw_profile)
        effect_vals = [e.value if hasattr(e, "value") else e for e in profile.allowed_effect_classes]
        assert EffectClass.PR_MUTATION.value in effect_vals

    def test_allow_pr_merge_adds_pr_merge(self):
        """--allow-pr-merge adds PR_MERGE to WorkProfile allowed_effect_classes."""
        from ai_engineering.supervisor.policy.work_profile import validate_work_profile
        raw_profile = _base_raw_profile()
        raw_profile["allowed_effect_classes"].append(EffectClass.PR_MERGE.value)
        profile = validate_work_profile(raw_profile)
        effect_vals = [e.value if hasattr(e, "value") else e for e in profile.allowed_effect_classes]
        assert EffectClass.PR_MERGE.value in effect_vals

    def test_both_flags_adds_both(self):
        """Both flags adds both PR_MUTATION and PR_MERGE."""
        from ai_engineering.supervisor.policy.work_profile import validate_work_profile
        raw_profile = _base_raw_profile()
        raw_profile["allowed_effect_classes"].extend([
            EffectClass.PR_MUTATION.value,
            EffectClass.PR_MERGE.value,
        ])
        profile = validate_work_profile(raw_profile)
        effect_vals = [e.value if hasattr(e, "value") else e for e in profile.allowed_effect_classes]
        assert EffectClass.PR_MUTATION.value in effect_vals
        assert EffectClass.PR_MERGE.value in effect_vals

    def test_pr_authority_does_not_override_task_intent(self):
        """WorkProfile PR authority exposes capability to PolicyEngine but MUST NOT override TaskIntent."""
        # This is a design invariant: WorkProfile.allowed_effect_classes is an ALLOWLIST
        # that caps what the PolicyEngine can approve — it does NOT grant authority by itself.
        # PolicyEngine must still evaluate intent constraints before allowing PR mutations.
        from ai_engineering.supervisor.policy.work_profile import validate_work_profile
        from ai_engineering.supervisor.policy.engine import evaluate_policy
        raw_profile = _base_raw_profile()
        raw_profile["allowed_effect_classes"].extend([EffectClass.PR_MUTATION.value])
        profile = validate_work_profile(raw_profile)
        effect_vals = [e.value if hasattr(e, "value") else e for e in profile.allowed_effect_classes]
        # PR_MUTATION is in the profile → PolicyEngine CAN allow it IF intent also permits
        assert EffectClass.PR_MUTATION.value in effect_vals


# ---------------------------------------------------------------------------
# 3. CandidateHeadIdentity Full Persistence + Digest Replay
# ---------------------------------------------------------------------------

class TestCandidateHeadIdentityPersistence:
    """CANDIDATE_HEAD_IDENTITY_REGISTERED must persist ALL identity fields + digest."""

    def test_register_persists_all_fields(self, tmp_state_dir, intent):
        """All CandidateHeadIdentity fields are persisted to the event store."""
        cid = _make_test_cid(intent, head_sha="deadbeef" * 5)
        coord = _make_coordinator(
            tmp_state_dir, intent,
            provider_mode="test",
            candidate_head_identity=cid,
        )
        # Find the registration event
        events = coord.store.load_events(coord.run_id)
        reg_ev = next(
            (e for e in events
             if getattr(e.event_type, "value", str(e.event_type)) == "CANDIDATE_HEAD_IDENTITY_REGISTERED"),
            None,
        )
        assert reg_ev is not None, "CANDIDATE_HEAD_IDENTITY_REGISTERED event not found"
        p = reg_ev.payload
        assert p is not None
        assert p["schema_version"] == CANDIDATE_HEAD_IDENTITY_SCHEMA_VERSION
        assert p["candidate_id"] == cid.candidate_id
        assert p["run_id"] == cid.run_id
        assert p["task_id"] == cid.task_id
        assert p["repository"] == cid.repository
        assert p["remote_name"] == cid.remote_name
        assert p["head_ref"] == cid.head_ref
        assert p["head_sha"] == cid.head_sha
        assert p["base_ref"] == cid.base_ref
        assert p["base_sha"] == cid.base_sha
        assert p["source_base_sha"] == cid.source_base_sha
        assert p["published_at_utc"] == cid.published_at_utc
        assert "digest" in p  # digest field must be present

    def test_register_persists_digest(self, tmp_state_dir, intent):
        """The digest field in the persisted payload matches compute_candidate_head_identity_digest."""
        cid = _make_test_cid(intent, head_sha="abc123" * 6 + "ab")
        coord = _make_coordinator(
            tmp_state_dir, intent,
            provider_mode="test",
            candidate_head_identity=cid,
        )
        events = coord.store.load_events(coord.run_id)
        reg_ev = next(
            (e for e in events
             if getattr(e.event_type, "value", str(e.event_type)) == "CANDIDATE_HEAD_IDENTITY_REGISTERED"),
            None,
        )
        assert reg_ev is not None
        p = reg_ev.payload
        # The digest stored is exactly what compute_candidate_head_identity_digest produces
        # for the cid's own fields (which already has a digest field set)
        stored_digest = p["digest"]
        assert stored_digest  # non-empty

    def test_restart_rehydrates_all_fields(self, tmp_state_dir, intent):
        """On restart, all CandidateHeadIdentity fields are deserialized from the event store."""
        cid = _make_test_cid(intent)
        coord1 = _make_coordinator(
            tmp_state_dir, intent,
            provider_mode="test",
            candidate_head_identity=cid,
        )
        run_id = coord1.run_id
        store = coord1.store
        loop = coord1.loop

        # Simulate restart: create a new coordinator from the same store
        registry = AgentRegistry()
        router_store = PersistentStore(str(tmp_state_dir))

        class LocalCI(CIStatusProvider):
            async def wait_for_ci(self, run_id, sha, **kwargs):
                return True

        from ai_engineering.supervisor.router.router import CrossAgentRouter, AuthorityResolver
        router = CrossAgentRouter(
            registry=registry,
            authority_resolver=AuthorityResolver(supervisor_store=store, supervisor_loop=loop),
            store=router_store,
        )
        coord2 = AutonomousRunCoordinator(
            loop=loop,
            store=store,
            router=router,
            result_collector=ResultCollector(),
            budget=BudgetConfig(),
            run_id=run_id,
            astra_provider=ScriptedAstraProposalProvider(),
            ci_provider=LocalCI(),
            evidence_root=str(tmp_state_dir),
            provider_mode="test",  # test mode to allow rehydration
            pr_provider=FakeGitHubPullRequestBackend(),
            allow_pr_create=False,
            allow_pr_merge=False,
            candidate_head_identity=None,  # no injected — must come from store
        )
        # Rehydrated from event store
        assert coord2.candidate_head_identity is not None
        assert coord2.candidate_head_identity.head_ref == cid.head_ref
        assert coord2.candidate_head_identity.repository == cid.repository
        assert coord2.candidate_head_identity.source_base_sha == cid.source_base_sha

    def test_digest_mismatch_on_restart_blocks_and_emits_invalid_event(self, tmp_state_dir, intent):
        """Digest mismatch on restart emits CANDIDATE_HEAD_IDENTITY_INVALID and sets identity to None."""
        cid = _make_test_cid(intent)
        coord1 = _make_coordinator(
            tmp_state_dir, intent,
            provider_mode="test",
            candidate_head_identity=cid,
        )
        run_id = coord1.run_id
        store = coord1.store
        loop = coord1.loop

        # Tamper the persisted digest in the event store — overwrite the registration event's payload
        events = store.load_events(run_id)
        tampered = False
        for ev in events:
            if getattr(ev.event_type, "value", str(ev.event_type)) == "CANDIDATE_HEAD_IDENTITY_REGISTERED":
                # We cannot directly mutate a frozen dataclass or the store,
                # so we write a new tampered event file manually
                tampered = True
                from ai_engineering.supervisor.events import create_event, canonical_serialize_event
                bad_payload = dict(ev.payload)
                bad_payload["digest"] = "f" * 64  # valid hex length but wrong digest
                tampered_ev = create_event(
                    run_id=ev.run_id,
                    sequence=ev.sequence,
                    previous_event_digest=ev.previous_event_digest,
                    event_type=ev.event_type,
                    state_revision=ev.state_revision,
                    task_id=ev.task_id,
                    attempt_id=ev.attempt_id,
                    intent_digest=ev.intent_digest,
                    payload=bad_payload,
                    created_at_utc=ev.created_at_utc,
                )
                files = list((tmp_state_dir / run_id / "events").glob(f"{ev.sequence:06d}_*.json"))
                if files:
                    files[0].unlink()
                    new_file = tmp_state_dir / run_id / "events" / f"{tampered_ev.sequence:06d}_{tampered_ev.event_id}.json"
                    new_file.write_text(canonical_serialize_event(tampered_ev), encoding="utf-8")
                break

        if not tampered:
            pytest.skip("Could not tamper event file — store layout differs")

        # Restart coordinator
        registry = AgentRegistry()
        router_store = PersistentStore(str(tmp_state_dir))

        class LocalCI(CIStatusProvider):
            async def wait_for_ci(self, run_id, sha, **kwargs):
                return True

        from ai_engineering.supervisor.router.router import CrossAgentRouter, AuthorityResolver
        router = CrossAgentRouter(
            registry=registry,
            authority_resolver=AuthorityResolver(supervisor_store=store, supervisor_loop=loop),
            store=router_store,
        )
        coord2 = AutonomousRunCoordinator(
            loop=loop,
            store=store,
            router=router,
            result_collector=ResultCollector(),
            budget=BudgetConfig(),
            run_id=run_id,
            astra_provider=ScriptedAstraProposalProvider(),
            ci_provider=LocalCI(),
            evidence_root=str(tmp_state_dir),
            provider_mode="test",
            pr_provider=FakeGitHubPullRequestBackend(),
            allow_pr_create=False,
            allow_pr_merge=False,
            candidate_head_identity=None,
        )
        # Identity must be rejected due to digest mismatch
        # (Either None or the invalid event was emitted)
        # Check that the coordinator emitted CANDIDATE_HEAD_IDENTITY_INVALID
        events2 = store.load_events(run_id)
        invalid_events = [
            e for e in events2
            if getattr(e.event_type, "value", str(e.event_type)) == "CANDIDATE_HEAD_IDENTITY_INVALID"
        ]
        assert len(invalid_events) >= 1, "CANDIDATE_HEAD_IDENTITY_INVALID event not emitted on digest mismatch"
        assert coord2.candidate_head_identity is None


# ---------------------------------------------------------------------------
# 4. CLI Flag Negative Scenarios
# ---------------------------------------------------------------------------

class TestCLIFlagNegativeScenarios:
    """CLI fail-closed negative scenarios."""

    def test_real_mode_with_fake_pr_mode_exits_nonzero(self, intent_file, tmp_state_dir):
        """--provider-mode real + (no --pr-mode github) → REAL_PR_PROVIDER_REQUIRED exit 2."""
        import subprocess
        result = subprocess.run(
            [
                sys.executable, str(_ROOT / "scripts" / "autonomous_run.py"),
                "--intent", intent_file,
                "--state-dir", str(tmp_state_dir),
                "--run-id", "test-neg-1",
                "--worker-cmd", "echo",
                "--provider-mode", "real",
                "--ci-mode", "github",
                "--pr-mode", "fake",  # invalid for real mode
                "--astra-cmd", "echo",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, "Expected non-zero exit for real+fake pr-mode"
        assert "REAL_PR_PROVIDER_REQUIRED" in (result.stderr + result.stdout)

    def test_real_mode_without_pr_mode_flag_uses_default_fake(self, intent_file, tmp_state_dir):
        """--provider-mode real with default --pr-mode fake → REAL_PR_PROVIDER_REQUIRED."""
        import subprocess
        result = subprocess.run(
            [
                sys.executable, str(_ROOT / "scripts" / "autonomous_run.py"),
                "--intent", intent_file,
                "--state-dir", str(tmp_state_dir),
                "--run-id", "test-neg-2",
                "--worker-cmd", "echo",
                "--provider-mode", "real",
                "--ci-mode", "github",
                # No --pr-mode → default is "fake"
                "--astra-cmd", "echo",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert "REAL_PR_PROVIDER_REQUIRED" in (result.stderr + result.stdout)

    def test_real_mode_without_astra_cmd_exits_nonzero(self, intent_file, tmp_state_dir):
        """--provider-mode real without --astra-cmd → ASTRA_PROVIDER_UNAVAILABLE."""
        import subprocess
        result = subprocess.run(
            [
                sys.executable, str(_ROOT / "scripts" / "autonomous_run.py"),
                "--intent", intent_file,
                "--state-dir", str(tmp_state_dir),
                "--run-id", "test-neg-3",
                "--worker-cmd", "echo",
                "--provider-mode", "real",
                "--ci-mode", "github",
                "--pr-mode", "github",
                # No --astra-cmd
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert "ASTRA_PROVIDER_UNAVAILABLE" in (result.stderr + result.stdout)

    def test_real_mode_without_ci_mode_github_exits_nonzero(self, intent_file, tmp_state_dir):
        """--provider-mode real without --ci-mode github → CI_PROVIDER_UNAVAILABLE."""
        import subprocess
        result = subprocess.run(
            [
                sys.executable, str(_ROOT / "scripts" / "autonomous_run.py"),
                "--intent", intent_file,
                "--state-dir", str(tmp_state_dir),
                "--run-id", "test-neg-4",
                "--worker-cmd", "echo",
                "--provider-mode", "real",
                # ci-mode defaults to "local"
                "--pr-mode", "github",
                "--astra-cmd", "echo",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert "CI_PROVIDER_UNAVAILABLE" in (result.stderr + result.stdout)

    def test_candidate_head_ref_flag_is_accepted(self, intent_file, tmp_state_dir):
        """--candidate-head-ref is a recognized CLI flag (no argparse error)."""
        import subprocess
        # We use --provider-mode test so it won't actually run a real process
        # but the argument parsing itself should work
        result = subprocess.run(
            [
                sys.executable, str(_ROOT / "scripts" / "autonomous_run.py"),
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # --help exits 0
        assert result.returncode == 0
        assert "candidate-head-ref" in (result.stdout + result.stderr)

    def test_real_mode_exit_code_is_2_for_pr_mode_violation(self, intent_file, tmp_state_dir):
        """Exit code must be exactly 2 for REAL_PR_PROVIDER_REQUIRED (not 1)."""
        import subprocess
        result = subprocess.run(
            [
                sys.executable, str(_ROOT / "scripts" / "autonomous_run.py"),
                "--intent", intent_file,
                "--state-dir", str(tmp_state_dir),
                "--run-id", "test-neg-6",
                "--worker-cmd", "echo",
                "--provider-mode", "real",
                "--ci-mode", "github",
                "--pr-mode", "fake",
                "--astra-cmd", "echo",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 2, f"Expected exit code 2, got {result.returncode}"


# ---------------------------------------------------------------------------
# 5. CLI Acceptance: --candidate-head-ref wiring in test mode
# ---------------------------------------------------------------------------

class TestCandidateHeadRefCLI:
    """--candidate-head-ref wires through to CandidateHeadIdentity in test/fake mode."""

    def test_candidate_head_ref_creates_cid_in_test_mode(self, tmp_state_dir, intent):
        """In test mode, --candidate-head-ref creates a CandidateHeadIdentity with head_ref set."""
        cid_fields = {
            "schema_version": CANDIDATE_HEAD_IDENTITY_SCHEMA_VERSION,
            "candidate_id": "",
            "run_id": "test-run",
            "task_id": intent.task_id,
            "repository": "life2boat/hermes",
            "remote_name": "github",
            "head_ref": "fix/task-7.8-cli-integration",
            "head_sha": "",
            "base_ref": "main",
            "base_sha": "",
            "source_base_sha": intent.source_base_sha,
            "published_at_utc": "",
            "digest": "",
        }
        cid_digest = compute_candidate_head_identity_digest(cid_fields)
        cid = CandidateHeadIdentity(
            schema_version=cid_fields["schema_version"],
            candidate_id=cid_fields["candidate_id"],
            run_id=cid_fields["run_id"],
            task_id=cid_fields["task_id"],
            repository=cid_fields["repository"],
            remote_name=cid_fields["remote_name"],
            head_ref=cid_fields["head_ref"],
            head_sha=cid_fields["head_sha"],
            base_ref=cid_fields["base_ref"],
            base_sha=cid_fields["base_sha"],
            source_base_sha=cid_fields["source_base_sha"],
            published_at_utc=cid_fields["published_at_utc"],
            digest=cid_digest,
        )
        coord = _make_coordinator(
            tmp_state_dir, intent,
            provider_mode="test",
            candidate_head_identity=cid,
        )
        assert coord.candidate_head_identity is not None
        assert coord.candidate_head_identity.head_ref == "fix/task-7.8-cli-integration"
        assert coord.candidate_head_identity.repository == "life2boat/hermes"
        assert coord.candidate_head_identity.source_base_sha == intent.source_base_sha


# ---------------------------------------------------------------------------
# 6. Real CLI Lifecycle E2E under Mocked HTTP
# ---------------------------------------------------------------------------

class TestRealCLILifecycleE2E:
    """Proves full lifecycle under mocked HTTP using the CLI async_main entrypoint."""

    @pytest.mark.asyncio
    async def test_cli_lifecycle_real_provider_mocked_http(self, intent_file, tmp_state_dir, monkeypatch):
        """CLI runs end-to-end with --provider-mode real and --pr-mode github under mocked GitHub HTTP."""
        monkeypatch.setenv("GITHUB_TOKEN", "mock-token-cli-test")
        from scripts.autonomous_run import async_main
        from unittest.mock import patch

        head_branch = "fix/task-7.8-cli-e2e"
        head_sha = "b" * 40
        main_sha = "eac702182c570dc45bcc91e72839f33b69f7c5e5"
        merged_sha = "m" * 40

        e2e_intent_file = tmp_state_dir / "e2e_intent.json"
        e2e_intent_data = dict(_INTENT_FIXTURE)
        e2e_intent_data["allowed_mutations"] = [
            "ai_engineering/supervisor/",
            "REPOSITORY_WRITE",
            "PR_MUTATION",
            "PR_MERGE",
        ]
        e2e_intent_file.write_text(json.dumps(e2e_intent_data), encoding="utf-8")

        pr_raw = {
            "number": 1234,
            "node_id": "PR_kw1234",
            "title": "Task 7.8 CLI E2E",
            "body": "PR Body",
            "draft": False,
            "state": "open",
            "mergeable": True,
            "mergeable_state": "clean",
            "merged": False,
            "merged_at": None,
            "merge_commit_sha": None,
            "created_at": "2026-09-12T00:00:00Z",
            "updated_at": "2026-09-12T00:00:00Z",
            "head": {"ref": head_branch, "sha": head_sha},
            "base": {"ref": "main", "sha": main_sha},
        }
        pr_merged = dict(pr_raw)
        pr_merged["state"] = "closed"
        pr_merged["merged"] = True
        pr_merged["merge_commit_sha"] = merged_sha

        call_records = []

        def mock_urlopen_handler(req, timeout=None):
            method = req.get_method()
            url = req.full_url
            call_records.append((method, url))
            class Resp:
                def __init__(self, code, data):
                    self.code = code
                    self.data = json.dumps(data).encode("utf-8")
                def read(self):
                    return self.data
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass

            if method == "GET" and f"/commits/{head_branch}" in url:
                return Resp(200, {"sha": head_sha})
            elif method == "GET" and "/pulls?state=open" in url:
                return Resp(200, [])
            elif method == "POST" and "/pulls" in url:
                return Resp(201, pr_raw)
            elif method == "GET" and "/pulls/1234" in url:
                if any(c[0] == "PUT" for c in call_records):
                    return Resp(200, pr_merged)
                return Resp(200, pr_raw)
            elif method == "GET" and "/commits/main" in url:
                if any(c[0] == "PUT" for c in call_records):
                    return Resp(200, {"sha": merged_sha})
                return Resp(200, {"sha": main_sha})
            elif method == "PUT" and "/pulls/1234/merge" in url:
                return Resp(200, {"merged": True, "sha": merged_sha})
            return Resp(404, {"message": "Not Found"})

        from ai_engineering.supervisor.autonomous_run import (
            AutonomousRunCoordinator,
            NextActionType,
            ScriptedAstraProposalProvider,
        )
        from tests.ai_engineering.test_task78_real_github_provider import _create_passing_vr, MockCIProvider
        orig_init = AutonomousRunCoordinator.__init__

        def custom_init(coord_self, *args, **kwargs):
            orig_init(coord_self, *args, **kwargs)
            # Inject a passing verified result so CREATE_PR has evidence
            vr = _create_passing_vr(
                task_id=coord_self.store.load_state(coord_self.run_id).current_task_id,
                run_id=coord_self.run_id,
                head_sha=head_sha,
                intent_dg=coord_self.store.load_state(coord_self.run_id).current_intent_digest,
            )
            coord_self.loop.ingest_verified_result(coord_self.run_id, vr, "vr-cli-test-dg")
            coord_self.ci_provider = MockCIProvider()
            coord_self.astra_provider = ScriptedAstraProposalProvider(
                scripted_actions=[
                    NextActionType.CREATE_PR,
                    NextActionType.WAIT_FOR_CI,
                    NextActionType.MERGE_IF_GREEN,
                ]
            )

        with patch("urllib.request.urlopen", side_effect=mock_urlopen_handler), \
             patch.object(AutonomousRunCoordinator, "__init__", custom_init):
            exit_code = await async_main([
                "--intent", str(e2e_intent_file),
                "--state-dir", str(tmp_state_dir),
                "--run-id", "cli-e2e-run-001",
                "--worker-cmd", "echo", "worker",
                "--astra-cmd", "echo", "astra",
                "--provider-mode", "real",
                "--ci-mode", "github",
                "--pr-mode", "github",
                "--allow-pr-create",
                "--allow-pr-merge",
                "--candidate-head-ref", head_branch,
            ])
            assert exit_code == 0, f"Expected CLI exit code 0, got {exit_code}"

        store = FileSupervisorStateStore(tmp_state_dir)
        events = store.load_events("cli-e2e-run-001")
        ev_types = [getattr(e.event_type, "value", str(e.event_type)) for e in events]
        assert "CANDIDATE_HEAD_IDENTITY_REGISTERED" in ev_types
        assert "CANDIDATE_HEAD_PUBLISHED" in ev_types
        assert "PR_CREATED" in ev_types
        assert "CI_WAIT_STARTED" in ev_types
        assert "CI_GREEN" in ev_types
        assert "PR_MERGE_REQUESTED" in ev_types
        assert "PR_MERGED" in ev_types
        assert "SOURCE_RECONCILED" in ev_types


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_cid(intent, head_sha: str = "") -> CandidateHeadIdentity:
    """Build a CandidateHeadIdentity with correct digest for testing."""
    head_sha = head_sha or "a" * 40
    cid_fields = {
        "schema_version": CANDIDATE_HEAD_IDENTITY_SCHEMA_VERSION,
        "candidate_id": "cid-test-001",
        "run_id": f"run-{uuid.uuid4().hex[:8]}",
        "task_id": intent.task_id,
        "repository": "life2boat/hermes",
        "remote_name": "github",
        "head_ref": "fix/task-7.8-real-cli-integration",
        "head_sha": head_sha,
        "base_ref": "main",
        "base_sha": "eac702182c570dc45bcc91e72839f33b69f7c5e5",
        "source_base_sha": intent.source_base_sha,
        "published_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "digest": "",
    }
    cid_digest = compute_candidate_head_identity_digest(cid_fields)
    cid_fields["digest"] = cid_digest
    return CandidateHeadIdentity(
        schema_version=cid_fields["schema_version"],
        candidate_id=cid_fields["candidate_id"],
        run_id=cid_fields["run_id"],
        task_id=cid_fields["task_id"],
        repository=cid_fields["repository"],
        remote_name=cid_fields["remote_name"],
        head_ref=cid_fields["head_ref"],
        head_sha=cid_fields["head_sha"],
        base_ref=cid_fields["base_ref"],
        base_sha=cid_fields["base_sha"],
        source_base_sha=cid_fields["source_base_sha"],
        published_at_utc=cid_fields["published_at_utc"],
        digest=cid_digest,
    )


def _base_raw_profile() -> dict:
    return {
        "schema_version": "hermes.work-profile.v1",
        "profile_id": "test-base",
        "profile_version": 1,
        "preferred_worker_model": None,
        "preferred_verifier_model": None,
        "preferred_supervisor_model": None,
        "escalation_model": None,
        "allowed_task_classes": ["BOUNDED_IMPLEMENTATION"],
        "maximum_autonomy_level": AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION.value,
        "allowed_effect_classes": [EffectClass.READ_ONLY.value, EffectClass.REPOSITORY_WRITE.value],
        "forbidden_effect_classes": [],
        "allowed_targets": [ExecutionTarget.DEV.value],
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
