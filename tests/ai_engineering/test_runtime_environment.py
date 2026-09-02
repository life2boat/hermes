"""PR-13 child environment sentinel tests (deny-by-default proof)."""

from __future__ import annotations

import json
import sys

from tests.ai_engineering.runtime_fixture_helpers import make_local_fixture, make_request

SENTINELS = {
    "GITHUB_TOKEN": "ghp_pr13_sentinel",
    "TELEGRAM_BOT_TOKEN": "tg_pr13_sentinel",
    "OPENAI_API_KEY": "sk-pr13_sentinel",
    "ANTHROPIC_API_KEY": "ak-pr13_sentinel",
    "HERMES_OBSERVABILITY_SECRET_SENTINEL_DO_NOT_EXPOSE": "obs_sentinel",
    "AWS_SECRET_ACCESS_KEY": "aws_pr13_sentinel",
    "SSH_PRIVATE_KEY": "ssh_pr13_sentinel",
    "DATABASE_URL": "postgres://pr13_sentinel",
}

PRINT_ENV = (
    "import json, os; print(json.dumps(sorted(k for k in os.environ "
    "if 'SENTINEL' in k or 'TOKEN' in k or 'API_KEY' in k or 'SECRET' in k or k == 'DATABASE_URL')))"
)


class TestChildEnvironmentDenyByDefault:
    def _child_env_keys(self, tmp_path):
        fx = make_local_fixture(
            tmp_path, parent_env_override={**SENTINELS, "PATH": "C:\\windows"}
        )
        evidence = fx.runtime.execute_agent_process(
            make_request(fx, argv=(sys.executable, "-c", PRINT_ENV), execution_id="exec-env"),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        assert evidence.exit_proven and evidence.exit_code == 0, evidence.to_dict()
        return evidence, json.loads(evidence.stdout.strip())

    def test_no_sentinel_secret_names_in_child_env(self, tmp_path):
        evidence, secret_keys = self._child_env_keys(tmp_path)
        assert secret_keys == []

    def test_sentinel_values_absent_from_evidence_stdout(self, tmp_path):
        evidence, _ = self._child_env_keys(tmp_path)
        for value in SENTINELS.values():
            assert value not in evidence.stdout

    def test_sentinels_absent_from_stderr(self, tmp_path):
        fx = make_local_fixture(
            tmp_path, parent_env_override={**SENTINELS, "PATH": "C:\\windows"}
        )
        evidence = fx.runtime.execute_agent_process(
            make_request(
                fx,
                argv=(sys.executable, "-c", "import os, sys; sys.stderr.write(repr(sorted(os.environ))); sys.exit(2)"),
                execution_id="exec-env-err",
            ),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        for name, value in SENTINELS.items():
            assert name not in evidence.stderr
            assert value not in evidence.stderr

    def test_benign_extra_reaches_child(self, tmp_path):
        fx = make_local_fixture(tmp_path, parent_env_override={"PATH": "C:\\windows"})
        fx.runtime._runner._parent_env = {"PATH": "C:\\windows"}
        fx.runtime._runner._extra_env = {"HERMES_PR13_MARKER": "ok"}
        evidence = fx.runtime.execute_agent_process(
            make_request(
                fx,
                argv=(sys.executable, "-c", "import os; print(os.environ.get('HERMES_PR13_MARKER', 'missing'))"),
                execution_id="exec-env-extra",
            ),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        assert "ok" in evidence.stdout

    def test_child_env_recorded_request_has_no_secrets(self, tmp_path):
        fx = make_local_fixture(
            tmp_path, parent_env_override={**SENTINELS, "PATH": "C:\\windows"}
        )
        evidence = fx.runtime.execute_agent_process(
            make_request(fx, argv=(sys.executable, "-c", "print('x')"), execution_id="exec-env-req"),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        request = fx.runtime.execution_registry.get_request(evidence.execution_id)
        assert request is not None
        assert request.inherit_environment is False
        for name in SENTINELS:
            assert name not in request.env
