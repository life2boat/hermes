from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "healbite-household-production-bootstrap.md"


def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _index(text: str, needle: str) -> int:
    idx = text.find(needle)
    assert idx >= 0, f"missing runbook marker: {needle}"
    return idx


def _bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, flags=re.S)


def test_runbook_defines_runtime_identity_and_permission_gate_contract() -> None:
    text = _text()
    required = [
        "## Runtime Identity and SQLite Permission Gate Contract",
        'ps -eo pid=,comm=,uid=,gid= --sort=pid',
        '[ "$comm" = "hermes" ]',
        'RUNTIME_UID',
        'RUNTIME_GID',
        'capture_db_permission_state',
        'verify_runtime_db_access',
        'verify_sqlite_sidecars',
        'run_db_permission_gate',
        'docker exec --user "${RUNTIME_UID}:${RUNTIME_GID}" "$EXACT_CONTAINER"',
        'file:/home/hermes/healbite.db?mode=ro',
        'PRAGMA query_only=ON',
        'PRAGMA quick_check',
        'journal_mode=',
    ]
    for needle in required:
        assert needle in text, f"missing runbook contract fragment: {needle}"


def test_runbook_orders_permission_gates_around_schema_and_bootstrap_steps() -> None:
    text = _text()
    order = [
        '## Stage 5A: Pre-Root-Write DB Permission Gate',
        '## Stage 6: Schema Authorization and Initialization',
        '## Stage 6A: Post-Schema Audit and Permission Gate',
        '## Stage 7: Eligibility Policy',
        '## Stage 8: Production Dry Run',
        '## Stage 9: First Bootstrap Apply',
        '## Stage 9A: Post-First Audit and Permission Gate',
        '## Stage 10: Second Bootstrap Apply',
        '## Stage 11: Canonical Audit',
        '## Stage 11A: Post-Second Permission Gate',
        '## Stage 12: Feature-Disabled Runtime Proof',
        '## Stage 14: Stability Window',
        '## Stage 14A: Final Stability Permission Gate',
    ]
    indices = [_index(text, marker) for marker in order]
    assert indices == sorted(indices)


def test_runbook_requires_container_only_permission_checks_and_exact_paths() -> None:
    text = _text()
    required = [
        'docker exec --user 0 "$EXACT_CONTAINER"',
        'docker exec --user "${RUNTIME_UID}:${RUNTIME_GID}" "$EXACT_CONTAINER"',
        '/opt/hermes/.venv/bin/python',
        '/home/hermes/healbite.db',
        'Do not use the host venv for authoritative permission checks.',
        'The runtime-user SQLite probe must remain read-only',
    ]
    for needle in required:
        assert needle in text, f"missing container-only permission probe requirement: {needle}"


def test_runbook_forbids_broad_permission_remediation_in_executable_flow() -> None:
    text = _text()
    assert '## SQLite Permission Remediation Policy' in text
    assert 'chown -R' in text
    assert 'chmod -R' in text
    assert 'chmod 777' in text
    assert 'chmod 666' in text

    forbidden = ('chown -R', 'chmod -R', 'chmod 777', 'chmod 666')
    for block in _bash_blocks(text):
        for needle in forbidden:
            assert needle not in block, f"forbidden remediation command leaked into executable flow: {needle}"


def test_runbook_requires_safe_evidence_and_sidecar_stop_triggers() -> None:
    text = _text()
    required = [
        '## SQLite Permission Evidence Contract',
        'runtime_uid',
        'runtime_gid',
        'db_owner_uid',
        'db_owner_gid',
        'db_mode',
        'db_parent_mode',
        'wal_present',
        'shm_present',
        'sidecars_compatible',
        'sqlite_permission_error_count',
        'DB inode/device',
        'Telegram IDs',
        'raw rows',
        'runtime UID/GID changed unexpectedly',
        'WAL or SHM became root-only or otherwise runtime-incompatible',
        'runtime SQLite read-only probe failed',
    ]
    for needle in required:
        assert needle in text, f"missing evidence or stop-trigger contract: {needle}"
