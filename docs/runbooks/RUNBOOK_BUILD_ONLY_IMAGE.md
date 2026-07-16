# Build-Only Hermes Image Contract

Use ops/build/build_hermes_image.sh when an operator needs one exact Hermes
image build without loading runtime configuration.

## Contract

The wrapper accepts exactly:

    build_hermes_image.sh IMAGE_TAG FULL_GIT_SHA

It fails closed unless:

- IMAGE_TAG is an explicit repository:tag reference;
- the tag is not latest, production, stable, or current;
- FULL_GIT_SHA is a 40-character lowercase hexadecimal revision;
- the revision matches the current worktree HEAD;
- the worktree is clean.

The wrapper performs exactly one operation:

    docker compose ... build hermes-bot

It does not pull, push, deploy, clean up, start, or recreate containers.

## Dotenv And Secret Isolation

The build uses only ops/build/docker-compose.build.yml. It does not include
the production Compose files.

Implicit dotenv loading is disabled twice:

- COMPOSE_DISABLE_ENV_FILE=1;
- docker compose --env-file /dev/null.

The standalone Compose file contains no env_file, runtime environment,
volumes, ports, networks, secrets, configs, database paths, Qdrant settings, or
Telegram settings. Its only interpolation inputs are the explicit image tag and
Git revision supplied to the wrapper.

The wrapper does not access the legacy secret override.

## Verification Without A Build

The static contract tests replace docker and git through a temporary test
PATH; no Docker build or container operation occurs:

    PYTHONDONTWRITEBYTECODE=1 PYTEST_ADDOPTS='-p no:cacheprovider' \
      python -m pytest -q tests/ops/test_build_only_compose_contract.py

A safe Compose rendering check must use the same dotenv controls and only the
standalone build-only Compose file. Do not save or print a full rendered
production configuration.
