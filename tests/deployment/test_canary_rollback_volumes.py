"""Regression tests for the automatic-rollback image-volume attestation fix.

Before this fix, _automatic_rollback called _post_deploy_attestation without
supplying image_declared_volume_destinations.  The keyword defaults to
frozenset(), meaning any anonymous volume created by the prior image caused
HERMES_MOUNT_SET_CHANGED during rollback attestation, escalating the result
code from ROLLED_BACK to post-deploy-rollback-failed.

These tests confirm the invariants after the fix:

    1. _automatic_rollback inspects the PRIOR (baseline) image, not the candidate.
    2. The prior image's declared_volume_destinations flows into post-deploy attestation.
    3. Candidate declared vols are NOT used in rollback path.
    4. canary_override=True still uses prior image metadata.
    5. Successful rollback returns a PostDeployAttestation object.
    6. A compose failure re-raises (fail-closed).
    7. The secret transaction is finalized even when attestation fails.
    8. The _post_deploy_attestation call site must pass the keyword explicitly.
    9. Prior image with no Config.Volumes passes frozenset() — not None.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_production_deploy as deploy  # noqa: E402

# deploy.attestation is the hermes_post_deploy_attestation module.
# Patch through deploy.attestation so monkeypatch targets the same binding
# that _automatic_rollback uses.
_attestation = deploy.attestation

# ── constants ──────────────────────────────────────────────────────────────────
IMAGE_PRIOR = "sha256:" + "a" * 64        # image running before activation
IMAGE_CANDIDATE = "sha256:" + "b" * 64   # canary candidate image

REVISION_PRIOR = "d" * 40
REVISION_CANDIDATE = "e" * 40

PRIOR_DECLARED_VOL = "/opt/data"
CANDIDATE_DECLARED_VOL = "/opt/cache"

TOKEN = "synthetic-secret-value"


# ── helpers ────────────────────────────────────────────────────────────────────

def _inspected(image_id: str, declared_vols: frozenset[str]) -> deploy.InspectedImage:
    revision = REVISION_PRIOR if image_id == IMAGE_PRIOR else REVISION_CANDIDATE
    return deploy.InspectedImage(
        image_id=image_id,
        revision=revision,
        declared_volume_destinations=declared_vols,
    )


def _minimal_baseline(
    image_id: str = IMAGE_PRIOR,
    revision: str = REVISION_PRIOR,
) -> SimpleNamespace:
    """A SimpleNamespace that satisfies _automatic_rollback BEFORE rollback_log_baseline.

    All tests must stub deploy.attestation.rollback_log_baseline to return the input
    unchanged, so the real dataclasses.replace() call is never reached with a
    SimpleNamespace (which is not a dataclass).
    """
    return SimpleNamespace(
        hermes=SimpleNamespace(image_id=image_id, revision=revision),
    )


def _base_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    prior_declared: frozenset[str] = frozenset([PRIOR_DECLARED_VOL]),
    candidate_declared: frozenset[str] = frozenset([CANDIDATE_DECLARED_VOL]),
) -> None:
    """Install stubs required by every test."""

    def _mock_inspect(_contract, image, *, expected_revision=None):
        vols = prior_declared if image == IMAGE_PRIOR else candidate_declared
        return _inspected(image, vols)

    monkeypatch.setattr(deploy, "inspect_local_image", _mock_inspect)
    # Patch through the module reference _automatic_rollback actually uses.
    monkeypatch.setattr(_attestation, "rollback_log_baseline", lambda bl: bl)
    monkeypatch.setattr(deploy, "_begin_secret_override_transaction", lambda *a, **k: object())
    monkeypatch.setattr(deploy, "_finish_secret_override_transaction", lambda *a, **k: None)


def _noop_compose(_contract, *, image_id, revision, canary_override=False):
    pass


def _pass_post(_c, _bl, *, target_image_id, target_revision, image_declared_volume_destinations):
    return SimpleNamespace(status="PASS")


# ── test 1 — rollback inspects PRIOR image ─────────────────────────────────────

def test_rollback_inspects_prior_image_not_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """_automatic_rollback must call inspect_local_image with the PRIOR image id."""
    inspected_images: list[str] = []

    def tracking_inspect(_contract, image, *, expected_revision=None):
        inspected_images.append(image)
        return _inspected(image, frozenset([PRIOR_DECLARED_VOL]))

    monkeypatch.setattr(deploy, "inspect_local_image", tracking_inspect)
    monkeypatch.setattr(_attestation, "rollback_log_baseline", lambda bl: bl)
    monkeypatch.setattr(deploy, "_begin_secret_override_transaction", lambda *a, **k: object())
    monkeypatch.setattr(deploy, "_finish_secret_override_transaction", lambda *a, **k: None)
    monkeypatch.setattr(deploy, "_compose_recreate_hermes", _noop_compose)
    monkeypatch.setattr(deploy, "_post_deploy_attestation", _pass_post)

    deploy._automatic_rollback(
        deploy.load_contract(),
        _minimal_baseline(image_id=IMAGE_PRIOR),
        {TOKEN: "x"},
    )

    assert inspected_images == [IMAGE_PRIOR], (
        f"Expected inspect_local_image called once with IMAGE_PRIOR; got {inspected_images}"
    )


# ── test 2 — prior declared volume flows into rollback attestation ─────────────

def test_prior_declared_volume_flows_into_rollback_attestation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prior image's declared_volume_destinations must reach _post_deploy_attestation."""
    received: list[tuple] = []

    def capturing_post(_c, _bl, *, target_image_id, target_revision, image_declared_volume_destinations):
        received.append((target_image_id, image_declared_volume_destinations))
        return SimpleNamespace(status="PASS")

    _base_stubs(monkeypatch, prior_declared=frozenset([PRIOR_DECLARED_VOL]))
    monkeypatch.setattr(deploy, "_compose_recreate_hermes", _noop_compose)
    monkeypatch.setattr(deploy, "_post_deploy_attestation", capturing_post)

    deploy._automatic_rollback(deploy.load_contract(), _minimal_baseline(), {TOKEN: "x"})

    assert len(received) == 1
    attested_image, attested_vols = received[0]
    assert attested_image == IMAGE_PRIOR
    assert attested_vols == frozenset([PRIOR_DECLARED_VOL])


# ── test 3 — candidate vols NOT used in rollback ───────────────────────────────

def test_candidate_declared_volumes_not_used_for_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rollback attestation must not receive the candidate image's volumes."""
    received_vols: list[frozenset] = []

    def capturing_post(_c, _bl, *, target_image_id, target_revision, image_declared_volume_destinations):
        received_vols.append(image_declared_volume_destinations)
        return SimpleNamespace(status="PASS")

    prior_vols = frozenset([PRIOR_DECLARED_VOL])
    candidate_vols = frozenset([CANDIDATE_DECLARED_VOL])
    _base_stubs(monkeypatch, prior_declared=prior_vols, candidate_declared=candidate_vols)
    monkeypatch.setattr(deploy, "_compose_recreate_hermes", _noop_compose)
    monkeypatch.setattr(deploy, "_post_deploy_attestation", capturing_post)

    deploy._automatic_rollback(deploy.load_contract(), _minimal_baseline(), {TOKEN: "x"})

    assert received_vols == [prior_vols]
    assert CANDIDATE_DECLARED_VOL not in received_vols[0]


# ── test 4 — canary_override still uses prior image metadata ───────────────────

def test_rollback_canary_override_still_uses_prior_image_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """canary_override=True must not change which image's volumes are used."""
    compose_flags: list[bool] = []
    received_vols: list[frozenset] = []

    def capturing_compose(_c, *, image_id, revision, canary_override=False):
        compose_flags.append(canary_override)

    def capturing_post(_c, _bl, *, target_image_id, target_revision, image_declared_volume_destinations):
        received_vols.append(image_declared_volume_destinations)
        return SimpleNamespace(status="PASS")

    prior_vols = frozenset([PRIOR_DECLARED_VOL])
    _base_stubs(monkeypatch, prior_declared=prior_vols)
    monkeypatch.setattr(deploy, "_compose_recreate_hermes", capturing_compose)
    monkeypatch.setattr(deploy, "_post_deploy_attestation", capturing_post)

    deploy._automatic_rollback(deploy.load_contract(), _minimal_baseline(), {TOKEN: "x"}, canary_override=True)

    assert compose_flags == [True], "compose must be called with canary_override=True"
    assert received_vols == [prior_vols]


# ── test 5 — rollback returns the PostDeployAttestation ───────────────────────

def test_rollback_returns_post_deploy_attestation(monkeypatch: pytest.MonkeyPatch) -> None:
    """_automatic_rollback must return the PostDeployAttestation from _post_deploy_attestation."""
    sentinel = SimpleNamespace(status="PASS", _marker="sentinel")

    _base_stubs(monkeypatch)
    monkeypatch.setattr(deploy, "_compose_recreate_hermes", _noop_compose)
    monkeypatch.setattr(deploy, "_post_deploy_attestation", lambda *a, **k: sentinel)

    result = deploy._automatic_rollback(deploy.load_contract(), _minimal_baseline(), {TOKEN: "x"})
    assert result is sentinel


# ── test 6 — compose failure re-raises (fail-closed) ──────────────────────────

def test_rollback_compose_failure_reraises_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If compose fails, _automatic_rollback must propagate the error without calling attestation."""
    attest_called: list[bool] = []

    def failing_compose(_c, *, image_id, revision, canary_override=False):
        raise deploy.DeploymentContractError("compose-up")

    def should_not_be_called(*a, **k):
        attest_called.append(True)
        return SimpleNamespace(status="PASS")

    _base_stubs(monkeypatch)
    monkeypatch.setattr(deploy, "_compose_recreate_hermes", failing_compose)
    monkeypatch.setattr(deploy, "_post_deploy_attestation", should_not_be_called)

    with pytest.raises(deploy.DeploymentContractError, match="compose-up"):
        deploy._automatic_rollback(deploy.load_contract(), _minimal_baseline(), {TOKEN: "x"})

    assert attest_called == [], "post-deploy attestation must not run after compose failure"


# ── test 7 — secret transaction finalized even when attestation fails ──────────

def test_secret_transaction_finalized_even_when_attestation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The secret transaction finally block must run regardless of attestation failure."""
    finish_called: list[bool] = []

    _base_stubs(monkeypatch)
    monkeypatch.setattr(deploy, "_finish_secret_override_transaction",
                        lambda *a, **k: finish_called.append(True))
    monkeypatch.setattr(deploy, "_compose_recreate_hermes", _noop_compose)
    monkeypatch.setattr(
        deploy, "_post_deploy_attestation",
        lambda *a, **k: (_ for _ in ()).throw(
            _attestation.RuntimeAttestationError("HERMES_MOUNT_SET_CHANGED")
        ),
    )

    with pytest.raises(_attestation.RuntimeAttestationError):
        deploy._automatic_rollback(deploy.load_contract(), _minimal_baseline(), {TOKEN: "x"})

    assert finish_called, "Secret transaction must be finalized even after attestation failure"


# ── test 8 — strict keyword contract at the call site ─────────────────────────

def test_post_deploy_attestation_call_site_passes_keyword_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A strict post mock (no **kwargs) must not TypeError.

    This test ensures the call site passes image_declared_volume_destinations
    as an explicit keyword argument and cannot silently omit it in future
    refactors.
    """
    received: list[frozenset] = []

    def strict_post(
        _contract,
        _baseline,
        *,
        target_image_id: str,
        target_revision: str,
        image_declared_volume_destinations: frozenset,
    ):
        received.append(image_declared_volume_destinations)
        return SimpleNamespace(status="PASS")

    _base_stubs(monkeypatch, prior_declared=frozenset([PRIOR_DECLARED_VOL]))
    monkeypatch.setattr(deploy, "_compose_recreate_hermes", _noop_compose)
    monkeypatch.setattr(deploy, "_post_deploy_attestation", strict_post)

    deploy._automatic_rollback(deploy.load_contract(), _minimal_baseline(), {TOKEN: "x"})

    assert received == [frozenset([PRIOR_DECLARED_VOL])]


# ── test 9 — empty declared volumes passes frozenset() not None ───────────────

def test_rollback_no_declared_volumes_passes_empty_frozenset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prior image with no Config.Volumes must pass frozenset() (never None)."""
    received_vols: list = []

    def strict_post(_c, _bl, *, target_image_id, target_revision, image_declared_volume_destinations):
        received_vols.append(image_declared_volume_destinations)
        return SimpleNamespace(status="PASS")

    _base_stubs(monkeypatch, prior_declared=frozenset())
    monkeypatch.setattr(deploy, "_compose_recreate_hermes", _noop_compose)
    monkeypatch.setattr(deploy, "_post_deploy_attestation", strict_post)

    deploy._automatic_rollback(deploy.load_contract(), _minimal_baseline(), {TOKEN: "x"})

    assert received_vols == [frozenset()], f"Expected [frozenset()], got {received_vols}"
    assert received_vols[0] is not None
