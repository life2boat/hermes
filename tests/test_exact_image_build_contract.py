from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / 'Dockerfile'
DOCKERIGNORE = REPO_ROOT / '.dockerignore'
COMPOSE_FILE = REPO_ROOT / 'docker-compose.yml'
RUNBOOK = REPO_ROOT / 'docs' / 'runbooks' / 'healbite-household-production-bootstrap.md'


def _text(path: Path) -> str:
    return path.read_text(encoding='utf-8')


def test_dockerfile_requires_exact_sha_and_embeds_revision_metadata() -> None:
    text = _text(DOCKERFILE)
    assert 'ARG HERMES_GIT_SHA' in text
    assert 'ARG HERMES_GIT_SHA=' not in text
    assert '40-character lowercase hex SHA' in text
    assert '/opt/hermes/.hermes_build_sha' in text
    assert 'chmod 0444 /opt/hermes/.hermes_build_sha' in text
    assert 'chown root:root /opt/hermes/.hermes_build_sha' in text
    assert 'LABEL org.opencontainers.image.revision="${HERMES_GIT_SHA}"' in text


def test_compose_exposes_immutable_image_and_fail_closed_build_arg() -> None:
    text = _text(COMPOSE_FILE)
    assert 'HERMES_GIT_SHA: ${HERMES_GIT_SHA:?' in text
    assert 'image: ${HERMES_IMAGE:-healbite-hermes:latest}' in text


def test_dockerignore_excludes_sensitive_runtime_and_private_inputs() -> None:
    text = _text(DOCKERIGNORE)
    required = [
        '*.db', '*.db-wal', '*.db-shm', '*.db-journal',
        '*.sqlite', '*.sqlite3', '*.sqlite-wal', '*.sqlite-shm',
        'backups/', 'private_backups/',
        'hermes-household-bootstrap-auth/',
        'hermes-household-bootstrap-eligible-users',
        '*.claimed',
        '.ssh/', 'id_rsa', 'id_ed25519', '*.pem', '*.key', '*.p12', '*.pfx',
        '.pytest_cache/', '__pycache__/', '*.pyc',
        'worktrees/',
    ]
    for pattern in required:
        assert pattern in text, f'missing dockerignore pattern: {pattern}'


def test_runbook_uses_exact_image_namespace_for_production_writes() -> None:
    text = _text(RUNBOOK)
    assert 'docker build \\' in text
    assert '--build-arg HERMES_GIT_SHA="$EXACT_SHA"' in text
    assert '--label org.opencontainers.image.revision="$EXACT_SHA"' in text
    assert 'HERMES_IMAGE="$IMAGE_REF" docker compose' in text
    assert 'docker exec --user 0 "$EXACT_CONTAINER" "$IMAGE_PYTHON" \\' in text
    assert '"$IMAGE_ROOT/scripts/household_db_audit.py"' in text
    assert '"$IMAGE_ROOT/scripts/household_bootstrap.py"' in text
    assert '/run/hermes-household-bootstrap-auth/' in text
    assert '/run/hermes-household-bootstrap-eligible-users' in text
    assert '/opt/hermes/.hermes_build_sha' in text
    assert 'Do not use the host venv for production writes.' in text
    assert '/home/hermes/.hermes/hermes-agent/venv/bin/python' not in text
