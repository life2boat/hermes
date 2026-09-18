"""
Tests for the configurable production evidence SSH port contract.

Statically verifies that release-qualification.yml:
- Has no hardcoded -p 22 in the production evidence collection step
- Requires PROD_SSH_PORT from secrets
- Contains fail-closed port validation
- Keeps StrictHostKeyChecking=yes
- Has no StrictHostKeyChecking=no
- Has no ssh-keyscan runtime TOFU
- Has HERMES_PROVENANCE_KEY absent from the collection step env
- Keeps PROD_SSH_KNOWN_HOSTS required
- Documents the [HOST]:PORT known_hosts format for non-default ports

No network calls are made; all assertions are purely textual/structural.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "release-qualification.yml"


@pytest.fixture(scope="module")
def workflow_text() -> str:
    assert WORKFLOW_PATH.exists(), f"Workflow file missing: {WORKFLOW_PATH}"
    return WORKFLOW_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def collect_step_text(workflow_text: str) -> str:
    """Extract only the 'Collect Canonical Production Evidence' step block."""
    lines = workflow_text.splitlines()
    start = None
    end = None
    for i, line in enumerate(lines):
        if "Collect Canonical Production Evidence" in line:
            start = i
        elif start is not None and re.match(r"      - name:", line):
            end = i
            break
    assert start is not None, "Could not find 'Collect Canonical Production Evidence' step"
    end = end or len(lines)
    return "\n".join(lines[start:end])


# ---------------------------------------------------------------------------
# Hardcoded port 22 absence
# ---------------------------------------------------------------------------

class TestHardcodedPortAbsent:
    def test_no_hardcoded_dash_p_22(self, collect_step_text: str) -> None:
        """The literal string '-p 22' must not appear in the collection step."""
        assert "-p 22" not in collect_step_text, (
            "Found hardcoded '-p 22' in the collection step — "
            "port must come from PROD_SSH_PORT secret."
        )

    def test_no_hardcoded_dash_p_22_anywhere_in_collection(self, collect_step_text: str) -> None:
        """Double-check: grep for the exact literal pattern with whitespace variants."""
        for pattern in ["-p 22 ", "-p 22\n", "-p 22'"]:
            assert pattern not in collect_step_text, (
                f"Found hardcoded port pattern {pattern!r} in collection step."
            )


# ---------------------------------------------------------------------------
# PROD_SSH_PORT required
# ---------------------------------------------------------------------------

class TestProdSshPortRequired:
    def test_prod_ssh_port_in_env_block(self, collect_step_text: str) -> None:
        """PROD_SSH_PORT must be declared in the step's env block."""
        assert "PROD_SSH_PORT:" in collect_step_text, (
            "PROD_SSH_PORT is not declared in the 'Collect Canonical Production Evidence' env block."
        )

    def test_prod_ssh_port_sourced_from_secrets(self, collect_step_text: str) -> None:
        """PROD_SSH_PORT value must come from secrets.PROD_SSH_PORT."""
        assert "secrets.PROD_SSH_PORT" in collect_step_text, (
            "PROD_SSH_PORT is not sourced from secrets.PROD_SSH_PORT."
        )

    def test_prod_ssh_port_used_in_ssh_command(self, collect_step_text: str) -> None:
        """The SSH invocation must use the variable ${PROD_SSH_PORT} or $PROD_SSH_PORT."""
        assert (
            '${PROD_SSH_PORT}' in collect_step_text
            or '"${PROD_SSH_PORT}"' in collect_step_text
            or "-p \"${PROD_SSH_PORT}\"" in collect_step_text
        ), "SSH command does not use PROD_SSH_PORT variable."


# ---------------------------------------------------------------------------
# Fail-closed port validation
# ---------------------------------------------------------------------------

class TestPortValidationFailClosed:
    def test_empty_port_rejected(self, collect_step_text: str) -> None:
        """Validation must catch empty PROD_SSH_PORT."""
        assert "-z" in collect_step_text, (
            "No empty-string check found for PROD_SSH_PORT."
        )

    def test_non_numeric_port_rejected(self, collect_step_text: str) -> None:
        """Validation must reject non-numeric values."""
        assert "^[0-9]" in collect_step_text, (
            "No numeric-only regex check found for PROD_SSH_PORT."
        )

    def test_zero_port_rejected(self, collect_step_text: str) -> None:
        """Port 0 must be explicitly rejected."""
        assert "-eq 0" in collect_step_text, (
            "No check for PROD_SSH_PORT == 0 found."
        )

    def test_over_65535_rejected(self, collect_step_text: str) -> None:
        """Port > 65535 must be explicitly rejected."""
        assert "65535" in collect_step_text, (
            "No upper-bound (65535) check found for PROD_SSH_PORT."
        )

    def test_whitespace_rejected(self, collect_step_text: str) -> None:
        """Whitespace-polluted port values must be rejected."""
        assert "[:space:]" in collect_step_text, (
            "No whitespace-detection check found for PROD_SSH_PORT."
        )

    def test_no_silent_default_to_22(self, collect_step_text: str) -> None:
        """The code must not silently fall back to 22 when PROD_SSH_PORT is missing."""
        # Confirm the validation exits explicitly when port is absent.
        assert "exit 1" in collect_step_text, (
            "No 'exit 1' found after port validation — fail-closed behavior unconfirmed."
        )


# ---------------------------------------------------------------------------
# SSH security contract
# ---------------------------------------------------------------------------

class TestSshSecurityContract:
    def test_strict_host_key_checking_yes(self, collect_step_text: str) -> None:
        """StrictHostKeyChecking must be 'yes' in the collection step."""
        assert "StrictHostKeyChecking=yes" in collect_step_text, (
            "StrictHostKeyChecking=yes is absent from the collection SSH command."
        )

    def test_no_strict_host_key_checking_no(self, workflow_text: str) -> None:
        """StrictHostKeyChecking=no must not appear anywhere in the workflow."""
        assert "StrictHostKeyChecking=no" not in workflow_text, (
            "Found StrictHostKeyChecking=no in the workflow — this is forbidden."
        )

    def test_no_ssh_keyscan_in_workflow(self, workflow_text: str) -> None:
        """Runtime ssh-keyscan (TOFU) must not appear in the workflow."""
        assert "ssh-keyscan" not in workflow_text, (
            "Found 'ssh-keyscan' in the qualification workflow — TOFU is forbidden."
        )

    def test_prod_ssh_known_hosts_required(self, collect_step_text: str) -> None:
        """PROD_SSH_KNOWN_HOSTS must still be required."""
        assert "PROD_SSH_KNOWN_HOSTS" in collect_step_text, (
            "PROD_SSH_KNOWN_HOSTS is no longer present in the collection step."
        )


# ---------------------------------------------------------------------------
# Provenance key isolation
# ---------------------------------------------------------------------------

class TestProvenanceKeyIsolation:
    def test_hermes_provenance_key_absent_from_collection_step(
        self, collect_step_text: str
    ) -> None:
        """HERMES_PROVENANCE_KEY must NOT appear in the collection step env."""
        assert "HERMES_PROVENANCE_KEY" not in collect_step_text, (
            "HERMES_PROVENANCE_KEY is present in the 'Collect Canonical Production Evidence' "
            "step — the signing key must never be exposed at the collection boundary."
        )


# ---------------------------------------------------------------------------
# Known-hosts port format documentation
# ---------------------------------------------------------------------------

class TestKnownHostsPortFormat:
    def test_bracket_notation_documented(self, collect_step_text: str) -> None:
        """The [HOST]:PORT bracket notation for non-standard ports must be documented."""
        assert "[HOST]:PORT" in collect_step_text or "[example.invalid]:" in collect_step_text, (
            "No [HOST]:PORT bracket-notation documentation found in the collection step. "
            "Operators need this guidance to provision PROD_SSH_KNOWN_HOSTS correctly "
            "for non-standard ports."
        )

    def test_no_real_host_key_in_workflow(self, workflow_text: str) -> None:
        """The real production host key must not be committed to the repository."""
        # The real WSL ed25519 key starts with this prefix
        assert "AAAAIIB7XfonwnJQNTejJecFjXU7WZ7Vb2LEDU0do0Sn0Tbu" not in workflow_text, (
            "Real production host key found in workflow file — secrets must never be in git."
        )
        # The wrong-host key from the failed attempt
        assert "AAAAIKT3MfMneHcesjXD4ZAEPu" not in workflow_text, (
            "Wrong host key from failed attempt found in workflow file."
        )
