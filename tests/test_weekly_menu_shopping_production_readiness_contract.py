from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"
PLAN = REPO_ROOT / "docs" / "design" / "healbite-weekly-menu-shopping-implementation-plan.md"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _index(text: str, needle: str) -> int:
    idx = text.find(needle)
    assert idx >= 0, f"missing marker: {needle}"
    return idx


def _bash_blocks(text: str) -> list[str]:
    return re.findall(r"```bash\n(.*?)```", text, flags=re.S)


def _require(text: str, needles: list[str], *, label: str) -> None:
    for needle in needles:
        assert needle in text, f"missing {label}: {needle}"


def test_runbook_defines_exact_image_and_service_isolation_contract() -> None:
    text = _text(RUNBOOK)
    _require(
        text,
        [
            "# HealBite Weekly Menu and Shopping Feature-Disabled Production Rollout",
            'EXACT_SHA="31f2594d2de352db3c0c6c78513770bdf5c606ab"',
            'IMAGE_REF="healbite-hermes:s71d1-${EXACT_SHA:0:12}"',
            'HERMES_SERVICE="hermes-bot"',
            'QDRANT_SERVICE="qdrant"',
            '/opt/hermes/.venv/bin/python',
            '/opt/hermes/.hermes_build_sha',
            '/home/hermes/healbite.db',
            'only hermes-bot may be recreated',
            'Qdrant must remain unchanged',
            '--no-deps --force-recreate hermes-bot',
            'docker inspect -f \'{{.Id}}\' "$QDRANT_SERVICE"',
        ],
        label="exact-image and service-isolation contract",
    )


def test_runbook_orders_future_phases_and_requires_weekly_before_shopping() -> None:
    text = _text(RUNBOOK)
    ordered = [
        '## D1 - Exact Image Build and Offline Validation',
        '## D2 - Feature-Disabled Hermes-Only Deployment',
        '## Backup Contract Before First Production DDL',
        '## D3 - Explicit Weekly Then Shopping Schema Initialization',
        '### D3 Weekly schema initialization',
        '### D3 Shopping schema initialization',
        '## D4 - Disabled-State Observation and Rollback Verification',
        '## D5 - Later Allowlist Canary',
    ]
    indices = [_index(text, marker) for marker in ordered]
    assert indices == sorted(indices)
    assert _index(text, '### D3 Weekly schema initialization') < _index(text, '### D3 Shopping schema initialization')
    assert _index(text, '## D4 - Disabled-State Observation and Rollback Verification') < _index(text, '## D5 - Later Allowlist Canary')


def test_runbook_requires_disabled_startup_and_no_auto_initialization_contract() -> None:
    text = _text(RUNBOOK)
    _require(
        text,
        [
            'HEALBITE_WEEKLY_MENU_ENABLED=false',
            'HEALBITE_WEEKLY_MENU_ALLOWLIST empty',
            'HEALBITE_SHOPPING_LIST_ENABLED=false',
            'HEALBITE_SHOPPING_LIST_ALLOWLIST empty',
            'gateway/run.py does not call weekly initialize_schema()',
            'gateway/run.py does not call shopping initialize_schema()',
            'missing weekly/shopping schema must not break disabled startup',
            'no weekly/shopping DDL occurs at startup',
            'no provider calls occur at startup',
        ],
        label="disabled-startup contract",
    )


def test_runbook_defines_backup_stop_go_and_zero_business_row_contracts() -> None:
    text = _text(RUNBOOK)
    _require(
        text,
        [
            '## Backup Contract Before First Production DDL',
            'backup must use SQLite backup API or an existing approved equivalent',
            'backup must record SHA-256',
            'restore test must target a separate temporary path',
            '### D3 Weekly schema initialization',
            '### D3 Shopping schema initialization',
            'series_count = 0',
            'revision_count = 0',
            'entry_count = 0',
            'list_count = 0',
            'item_count = 0',
            'idempotency_count = 0',
            '### STOP before deploy',
            '### STOP after image deploy',
            '### STOP after schema init',
            '## Evidence Template',
        ],
        label="backup / stop-go / zero-row contract",
    )


def test_runbook_separates_image_and_db_rollbacks_and_forbids_destructive_shell_flow() -> None:
    text = _text(RUNBOOK)
    _require(
        text,
        [
            '### Image-only rollback',
            '### Post-schema image rollback',
            '### DB rollback',
            'Destructive `DROP TABLE` is prohibited as ordinary rollback.',
        ],
        label="rollback taxonomy",
    )
    forbidden = ('docker compose down', 'docker system prune', 'DROP TABLE', 'git reset --hard', 'git clean', 'force push')
    for block in _bash_blocks(text):
        for needle in forbidden:
            assert needle not in block, f"forbidden command leaked into executable runbook block: {needle}"


def test_runbook_requires_staged_copy_and_disables_production_execution() -> None:
    text = _text(RUNBOOK)
    _require(
        text,
        [
            "staged copy plus atomic publish",
            "Direct in-place",
            "scripts/hermes_staged_schema_migrate.py",
            "production execution is disabled",
            "production DB path and production parent are not mounted",
            "PATH_MODE=STAGED_COPY",
            "normal SQLite DELETE journaling and synchronous FULL remain enabled",
            "os.replace",
            "cross-filesystem publication fails closed",
            "PUBLISH_STATE=UNKNOWN",
            "Backup restore is an emergency manual action only",
        ],
        label="staged-copy migration contract",
    )
    assert '--mount type=bind,src="/home/hermes/healbite.db"' not in text


def test_runbook_prohibits_sensitive_ids_and_secrets() -> None:
    text = _text(RUNBOOK)
    _require(
        text,
        [
            'Telegram IDs',
            'application user IDs',
            'household IDs',
            'member IDs',
            'allowlist contents',
            'profile values',
            'nutrition targets',
            'API keys or secrets',
            'production IDs prohibited',
        ],
        label="privacy prohibitions",
    )


def test_implementation_plan_records_d0_to_d5_separation_and_c5a_status() -> None:
    text = _text(PLAN)
    _require(
        text,
        [
            'Confirmed post-C5A merge baseline:',
            'C5A merged in `main` at `31f2594d2de352db3c0c6c78513770bdf5c606ab`;',
            'production remains on old revision `04566a0dd2b79f60748194cc3d318c5a5e75f3d3`;',
            'Telegram mutation UI is still undeployed;',
            'feature enablement remains a separate future canary stage.',
            '### D0 - Feature-Disabled Production Readiness Audit and Rollout Plan',
            '### D1 - Exact Image Build and Offline Validation',
            '### D2 - Feature-Disabled Hermes-Only Deployment',
            '### D3 - Explicit Weekly/Shopping Production Schema Initialization',
            '### D4 - Disabled-State Observation and Rollback Verification',
            '### D5 - Later Allowlist Canary',
        ],
        label="implementation plan D0-D5 status",
    )
