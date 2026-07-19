from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.secret_scanner import (
    SecretScanError,
    scan_secret_bytes,
    scan_secret_text,
)
from tests.secret_scanner_support import (
    marker_only_private_key_fixture,
    redaction_pattern_fixture,
    synthetic_assignment,
    synthetic_credential_url,
    synthetic_high_entropy_value,
    synthetic_private_key_block,
    synthetic_provider_token,
)


@pytest.mark.parametrize(
    "text",
    [
        "GEMINI_API_KEY\n",
        redaction_pattern_fixture(),
        "".join(("API_KEY", ' = "<API_KEY>"')),
        marker_only_private_key_fixture(),
        'replacement = "REDACTED"',
    ],
)
def test_legitimate_security_markers_are_accepted(text: str) -> None:
    assert scan_secret_text(text) == ()


@pytest.mark.parametrize(
    ("text", "expected_rule"),
    [
        (
            synthetic_assignment(),
            "provider-token-assignment",
        ),
        (
            synthetic_assignment(key="TOKEN"),
            "provider-token-assignment",
        ),
        (
            synthetic_assignment(
                key="SECRET",
                value=synthetic_high_entropy_value(),
            ),
            "high-entropy-secret-assignment",
        ),
        (
            synthetic_private_key_block(),
            "private-key-block",
        ),
        (
            synthetic_credential_url(),
            "credential-bearing-url",
        ),
        (
            "".join((
                'API_KEY = "<API_KEY>"\n',
                synthetic_assignment(key="BACKUP_TOKEN"),
            )),
            "provider-token-assignment",
        ),
    ],
)
def test_real_secret_shapes_are_denied(
    text: str,
    expected_rule: str,
) -> None:
    assert expected_rule in {finding.rule_id for finding in scan_secret_text(text)}


def test_binary_secret_marker_is_denied() -> None:
    data = b"\x00\xff" + synthetic_assignment().encode()
    assert scan_secret_bytes(data)


def test_non_binary_decode_failure_fails_closed() -> None:
    with pytest.raises(
        SecretScanError,
        match="^SECRET_SCAN_DECODING_FAILED$",
    ):
        scan_secret_bytes(b"\xff\xfeinvalid-text")


def test_text_and_bytes_callers_share_classification_policy() -> None:
    fixtures = (
        redaction_pattern_fixture(),
        marker_only_private_key_fixture(),
        synthetic_assignment(),
        synthetic_private_key_block(),
        synthetic_credential_url(),
    )
    for fixture in fixtures:
        assert scan_secret_bytes(fixture.encode()) == scan_secret_text(fixture)


def test_repository_and_context_callers_import_shared_policy() -> None:
    root = Path(__file__).resolve().parents[2]
    repository_scanner = (root / "scripts" / "secret_check.sh").read_text(
        encoding="utf-8"
    )
    context_scanner = (
        root / "scripts" / "build_verified_playwright_image.py"
    ).read_text(encoding="utf-8")

    assert "from scripts.secret_scanner import scan_secret_text" in (repository_scanner)
    assert "from scripts.secret_scanner import SecretScanError, scan_secret_bytes" in (
        context_scanner
    )
    assert "patterns = [" not in repository_scanner


@pytest.mark.parametrize(
    "fixture",
    [
        redaction_pattern_fixture(),
        marker_only_private_key_fixture(),
        synthetic_assignment(),
        synthetic_private_key_block(),
        synthetic_credential_url(),
    ],
)
def test_repository_and_context_callers_classify_shared_fixtures_identically(
    tmp_path: Path,
    fixture: str,
) -> None:
    source_root = Path(__file__).resolve().parents[2]
    repository = tmp_path / "repository"
    scripts_dir = repository / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(
        source_root / "scripts" / "secret_check.sh",
        scripts_dir / "secret_check.sh",
    )
    shutil.copy2(
        source_root / "scripts" / "secret_scanner.py",
        scripts_dir / "secret_scanner.py",
    )

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(arguments),
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    assert run("git", "init", "--quiet").returncode == 0
    assert run("git", "config", "user.name", "Synthetic Test").returncode == 0
    assert (
        run(
            "git",
            "config",
            "user.email",
            "synthetic@example.invalid",
        ).returncode
        == 0
    )
    assert run("git", "add", "scripts").returncode == 0
    assert run("git", "commit", "--quiet", "-m", "scanner baseline").returncode == 0
    candidate = repository / "candidate.txt"
    candidate.write_text(fixture, encoding="utf-8")
    assert run("git", "add", "candidate.txt").returncode == 0

    completed = run("bash", "scripts/secret_check.sh")
    repository_denied = completed.returncode != 0
    context_denied = bool(scan_secret_bytes(fixture.encode()))

    assert repository_denied is context_denied
    assert fixture not in completed.stdout
    assert fixture not in completed.stderr
