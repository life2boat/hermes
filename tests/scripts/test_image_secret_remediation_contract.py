from __future__ import annotations

import copy
import hashlib
import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts import hermes_image_secret_scan as scanner
from scripts.secret_scanner import SecretFinding


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPOSITORY_ROOT / "deploy" / "hermes-image-secret-exceptions.json"


def _load_policy_payload() -> dict[str, object]:
    return json.loads(POLICY_PATH.read_text(encoding="utf-8"))


def _write_policy(tmp_path: Path, payload: dict[str, object]) -> Path:
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    path = deploy / POLICY_PATH.name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _mutated_policy(
    tmp_path: Path,
    mutation: Callable[[dict[str, object]], None],
) -> Path:
    payload = copy.deepcopy(_load_policy_payload())
    mutation(payload)
    return _write_policy(tmp_path, payload)


def _first_exception(payload: dict[str, object]) -> dict[str, object]:
    exceptions = payload["exceptions"]
    assert isinstance(exceptions, list)
    first = exceptions[0]
    assert isinstance(first, dict)
    return first


def _matching_finding(exception: scanner.ImageSecretException) -> SecretFinding:
    rule_id = exception.rule_ids[0]
    return SecretFinding(rule_id, scanner.EXCEPTION_RULE_CLASSES[rule_id])


def test_repository_exception_policy_has_exactly_29_closed_scopes() -> None:
    policy = scanner.load_exception_policy(REPOSITORY_ROOT)

    assert len(policy.exceptions) == 29
    assert len({item.exception_id for item in policy.exceptions}) == 29
    assert all("*" not in item.normalized_path for item in policy.exceptions)
    assert all("?" not in item.normalized_path for item in policy.exceptions)
    assert all(item.rule_ids for item in policy.exceptions)
    assert all("*" not in rule for item in policy.exceptions for rule in item.rule_ids)
    assert any(
        item.private_key_shape == "WELL_FORMED_PEM" for item in policy.exceptions
    )


def test_valid_exception_suppresses_only_its_bound_finding() -> None:
    exception = scanner.load_exception_policy(REPOSITORY_ROOT).exceptions[0]

    remaining = scanner._apply_finding_exceptions(
        [_matching_finding(exception)],
        normalized_path=exception.normalized_path.removeprefix("/"),
        file_sha256=exception.file_sha256,
        private_key_shape=exception.private_key_shape,
        policy=scanner.ExceptionPolicy((exception,)),
    )

    assert remaining == []


def test_changed_file_hash_rejects_exception() -> None:
    exception = scanner.load_exception_policy(REPOSITORY_ROOT).exceptions[0]

    remaining = scanner._apply_finding_exceptions(
        [_matching_finding(exception)],
        normalized_path=exception.normalized_path.removeprefix("/"),
        file_sha256="0" * 64,
        private_key_shape=exception.private_key_shape,
        policy=scanner.ExceptionPolicy((exception,)),
    )

    assert remaining == [_matching_finding(exception)]


def test_rule_id_mismatch_rejects_exception() -> None:
    exception = scanner.load_exception_policy(REPOSITORY_ROOT).exceptions[0]
    finding = SecretFinding("different-specific-rule", "PRIVATE_KEY_MATERIAL")

    remaining = scanner._apply_finding_exceptions(
        [finding],
        normalized_path=exception.normalized_path.removeprefix("/"),
        file_sha256=exception.file_sha256,
        private_key_shape=exception.private_key_shape,
        policy=scanner.ExceptionPolicy((exception,)),
    )

    assert remaining == [finding]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("package_name", "different-package"),
        ("package_version", "999.0"),
        ("artifact_identity", "sha256:" + "f" * 64),
    ],
)
def test_package_binding_mismatch_rejected_at_load_time(
    tmp_path: Path, field: str, replacement: str
) -> None:
    def mutate(payload: dict[str, object]) -> None:
        _first_exception(payload)[field] = replacement

    _mutated_policy(tmp_path, mutate)

    with pytest.raises(
        scanner.ImageScanError, match="IMAGE_EXCEPTION_ARTIFACT_BINDING_MISMATCH"
    ):
        scanner.load_exception_policy(tmp_path)


@pytest.mark.parametrize(
    "field",
    [
        "package_name",
        "package_version",
        "artifact_identity",
        "classification",
        "private_key_shape",
    ],
)
def test_malformed_exception_scalar_is_fail_closed(tmp_path: Path, field: str) -> None:
    def mutate(payload: dict[str, object]) -> None:
        _first_exception(payload)[field] = {"unexpected": "object"}

    _mutated_policy(tmp_path, mutate)

    with pytest.raises(scanner.ImageScanError, match="IMAGE_EXCEPTION_POLICY_INVALID"):
        scanner.load_exception_policy(tmp_path)


def test_version_range_artifact_binding_is_rejected(tmp_path: Path) -> None:
    def mutate(payload: dict[str, object]) -> None:
        bindings = payload["artifact_bindings"]
        assert isinstance(bindings, list)
        binding = bindings[0]
        assert isinstance(binding, dict)
        binding["package_version"] = "~1.0"
        _first_exception(payload)["package_version"] = "~1.0"

    _mutated_policy(tmp_path, mutate)

    with pytest.raises(
        scanner.ImageScanError, match="IMAGE_EXCEPTION_ARTIFACT_BINDING_INVALID"
    ):
        scanner.load_exception_policy(tmp_path)


def test_path_hash_mismatch_rejected_at_load_time(tmp_path: Path) -> None:
    def mutate(payload: dict[str, object]) -> None:
        _first_exception(payload)["path_sha256"] = "0" * 64

    _mutated_policy(tmp_path, mutate)

    with pytest.raises(
        scanner.ImageScanError, match="IMAGE_EXCEPTION_PATH_HASH_MISMATCH"
    ):
        scanner.load_exception_policy(tmp_path)


def test_wildcard_path_rejected_at_load_time(tmp_path: Path) -> None:
    def mutate(payload: dict[str, object]) -> None:
        _first_exception(payload)["normalized_path"] = "/opt/hermes/*"

    _mutated_policy(tmp_path, mutate)

    with pytest.raises(scanner.ImageScanError, match="IMAGE_EXCEPTION_PATH_INVALID"):
        scanner.load_exception_policy(tmp_path)


def test_different_finding_class_does_not_inherit_exception() -> None:
    exception = scanner.load_exception_policy(REPOSITORY_ROOT).exceptions[0]
    finding = SecretFinding(exception.rule_ids[0], "PROTECTED_SECRET_MATERIAL")

    remaining = scanner._apply_finding_exceptions(
        [finding],
        normalized_path=exception.normalized_path.removeprefix("/"),
        file_sha256=exception.file_sha256,
        private_key_shape=exception.private_key_shape,
        policy=scanner.ExceptionPolicy((exception,)),
    )

    assert remaining == [finding]


def test_well_formed_pem_does_not_match_marker_only_exception() -> None:
    policy = scanner.load_exception_policy(REPOSITORY_ROOT)
    exception = next(
        item for item in policy.exceptions if item.private_key_shape == "MARKER_ONLY"
    )

    remaining = scanner._apply_finding_exceptions(
        [_matching_finding(exception)],
        normalized_path=exception.normalized_path.removeprefix("/"),
        file_sha256=exception.file_sha256,
        private_key_shape="WELL_FORMED_PEM",
        policy=scanner.ExceptionPolicy((exception,)),
    )

    assert remaining == [_matching_finding(exception)]


def test_group_b_source_files_have_no_scanner_findings() -> None:
    protected_names = ("GEMINI_API_KEY", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN")
    paths = (
        "agent/redact.py",
        "scripts/hermes_image_secret_scan.py",
        "scripts/secret_scanner.py",
        "scripts/healbite_status.py",
        "scripts/setup_open_webui.sh",
    )

    for relative in paths:
        data = (REPOSITORY_ROOT / relative).read_bytes()
        findings = scanner._scan_stream(
            io.BytesIO(data), protected_names=protected_names, exact_values=()
        )[0]
        assert findings == [], relative


def test_setup_open_webui_preserves_generated_export_without_source_assignment() -> (
    None
):
    text = (REPOSITORY_ROOT / "scripts/setup_open_webui.sh").read_text(encoding="utf-8")

    assert 'openai_key_name="OPENAI_API""_KEY"' in text
    assert 'export ${openai_key_name}="\\$API_KEY"' in text
    assert "export OPENAI_API_KEY=" not in text


def _docker_instructions() -> list[str]:
    text = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        current = f"{current} {line}".strip()
        if not line.endswith("\\"):
            instructions.append(current)
            current = ""
    assert not current
    return instructions


def test_group_c_cleanup_is_bound_to_creating_instructions() -> None:
    instructions = _docker_instructions()
    npm_install = next(
        item for item in instructions if item.startswith("RUN npm install")
    )
    uv_sync = next(item for item in instructions if item.startswith("RUN uv sync"))
    playwright = next(
        item
        for item in instructions
        if item.startswith("RUN --mount=type=bind,from=playwright_artifacts")
    )
    node_source_cleanup = next(
        item
        for item in instructions
        if item.startswith("RUN rm -f /usr/local/lib/node_modules/npm/docs/")
    )

    expected_by_instruction = {
        node_source_cleanup: (
            "/usr/local/lib/node_modules/npm/docs/content/using-npm/config.md",
            "/usr/local/lib/node_modules/npm/docs/output/using-npm/config.html",
            "/usr/local/lib/node_modules/npm/man/man7/config.7",
            "/usr/local/lib/node_modules/npm/node_modules/@npmcli/arborist/README.md",
        ),
        npm_install: ("/tmp/node-compile-cache/v22.22.3-x64-9ac5647c-0/0c92995d",),
        uv_sync: (
            "/opt/hermes/.venv/lib/python3.13/site-packages/Crypto/SelfTest/Cipher/test_pkcs1_15.py",
            "/opt/hermes/.venv/lib/python3.13/site-packages/Crypto/SelfTest/Protocol/test_ecdh.py",
            "/opt/hermes/.venv/lib/python3.13/site-packages/Crypto/SelfTest/PublicKey/test_import_DSA.py",
            "/opt/hermes/.venv/lib/python3.13/site-packages/Crypto/SelfTest/PublicKey/test_import_ECC.py",
            "/opt/hermes/.venv/lib/python3.13/site-packages/Crypto/SelfTest/PublicKey/test_import_RSA.py",
            "/opt/hermes/.venv/lib/python3.13/site-packages/Crypto/SelfTest/Signature/test_pkcs1_15.py",
            "/opt/hermes/.venv/lib/python3.13/site-packages/Crypto/SelfTest/Signature/test_pss.py",
            "/opt/hermes/.venv/lib/python3.13/site-packages/tornado/test/test.key",
            "/opt/hermes/.venv/lib/python3.13/site-packages/youtube_transcript_api/test/assets/youtube.html.static",
            "/opt/hermes/.venv/lib/python3.13/site-packages/botocore/data/iam/2010-05-08/examples-1.json",
        ),
        playwright: (
            "/var/lib/apt/lists/deb.debian.org_debian_dists_trixie_main_binary-amd64_Packages.lz4",
        ),
    }

    for instruction, paths in expected_by_instruction.items():
        assert "rm -rf" not in instruction
        for path in paths:
            assert f"rm -f {path}" in instruction
            assert "*" not in path
            assert "?" not in path


def test_full_scanner_still_detects_high_entropy_and_protected_material() -> None:
    credential = b"Qw9Zx7Cv5Bn3Mk8Jh6Gf2Ds4Pa7Rt"
    protected = b"OPENAI_API_KEY=" + credential
    high_entropy = b'password = "' + credential + b'"'

    protected_findings = scanner._scan_bytes(
        protected, protected_names=("OPENAI_API_KEY",), exact_values=()
    )
    entropy_findings = scanner._scan_bytes(
        high_entropy, protected_names=(), exact_values=()
    )

    assert any(
        item.rule_id == "protected-secret-assignment" for item in protected_findings
    )
    assert any(
        item.rule_id == "high-entropy-secret-assignment" for item in entropy_findings
    )


def test_exception_file_sha_values_are_not_path_hashes() -> None:
    policy = scanner.load_exception_policy(REPOSITORY_ROOT)

    assert all(
        item.file_sha256
        != hashlib.sha256(
            item.normalized_path.removeprefix("/").encode("utf-8")
        ).hexdigest()
        for item in policy.exceptions
    )
