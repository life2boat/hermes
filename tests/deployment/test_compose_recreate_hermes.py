import pytest
from pathlib import Path
from scripts import hermes_production_deploy as deploy
import json

def test_compose_recreate_hermes_normal_call(monkeypatch):
    contract = deploy.load_contract()

    run_calls = []
    class FakeRes:
        def __init__(self, rc, out=""):
            self.returncode = rc
            self.stdout = out
    def fake_run(cmd, *a, **kw):
        run_calls.append(cmd)
        return FakeRes(0)

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy, "_compose_environment", lambda *a: {})

    deploy._compose_recreate_hermes(
        contract,
        image_id="fake_image_id",
        revision="fake_revision",
    )

    assert len(run_calls) == 1
    assert "up" in run_calls[0]
    assert "--force-recreate" in run_calls[0]


def test_compose_recreate_hermes_requires_replacement_success(monkeypatch):
    contract = deploy.load_contract()

    run_calls = []
    class FakeRes:
        def __init__(self, rc, out=""):
            self.returncode = rc
            self.stdout = out

    def fake_run(cmd, *a, **kw):
        run_calls.append(cmd)
        if "inspect" in cmd:
            return FakeRes(0, '[{"Id": "new_id"}]')
        return FakeRes(0)

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy, "_compose_environment", lambda *a: {})

    deploy._compose_recreate_hermes(
        contract,
        image_id="fake_image_id",
        revision="fake_revision",
        require_replacement=True,
        original_container_id="old_id",
    )

    assert len(run_calls) == 2


def test_compose_recreate_hermes_requires_replacement_fails_on_reuse(monkeypatch):
    contract = deploy.load_contract()

    run_calls = []
    class FakeRes:
        def __init__(self, rc, out=""):
            self.returncode = rc
            self.stdout = out

    def fake_run(cmd, *a, **kw):
        run_calls.append(cmd)
        if "inspect" in cmd:
            return FakeRes(0, '[{"Id": "old_id"}]')
        return FakeRes(0)

    monkeypatch.setattr(deploy, "_run", fake_run)
    monkeypatch.setattr(deploy, "_compose_environment", lambda *a: {})

    with pytest.raises(deploy.DeploymentContractError, match="original-container-reuse-rejected"):
        deploy._compose_recreate_hermes(
            contract,
            image_id="fake_image_id",
            revision="fake_revision",
            require_replacement=True,
            original_container_id="old_id",
        )
