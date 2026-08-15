"""Canonical constants for HealBite secret remediation R1."""

# Canonical protected secret variable names
PROTECTED_NAMES: frozenset[str] = frozenset({
    "TELEGRAM_BOT_TOKEN",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "NOUS_API_KEY",
    "OPENAI_API_KEY",
    "QWEN_API_KEY",
})

REQUIRED_SECRET_NAMES: frozenset[str] = frozenset({"TELEGRAM_BOT_TOKEN"})

# Canonical production runtime paths
PROD_LEGACY_ENV_PATH = "/home/hermes/.hermes/.env"
PROD_SECRET_FILE_PATH = "/etc/hermes/hermes-production.env"
PROD_RUNTIME_ENV_PATH = "/etc/hermes/hermes-runtime.env"
PROD_PARENT_DIR_PATH = "/etc/hermes"

# Canonical production container identity
CONTAINER_NAME = "hermes-bot"
COMPOSE_PROJECT = "healbite-s72-family-invite-main"
COMPOSE_SERVICE = "hermes-bot"
LEGACY_IMAGE_REF = "healbite-hermes:pr99-main-273b0a6cccaf"
LEGACY_IMAGE_ID = "sha256:635efcd80ac8326848ed3620d5d9878971b224076c4f8694d5c22d1edfe1ed08"

# Canonical Compose working dir
COMPOSE_WORKDIR = "/home/hermes/.hermes/worktrees/healbite-s72-family-invite-main"

# Exact ordered Compose files
COMPOSE_FILES = [
    "/home/hermes/.hermes/worktrees/healbite-s72-family-invite-main/docker-compose.yml",
    "/home/hermes/.hermes/worktrees/healbite-s72-family-invite-main/deploy/docker-compose.production.yml",
    "/root/hermes_restore/root/hermes-migration-plans/pr99/20260728T003728Z/inputs/production-db-override.json",
    "/root/hermes_restore/run/hermes/hermes-secrets-override.yml",
    "/root/hermes_restore/run/hermes-weekly-draft-canary.yml",
    "/root/hermes_restore/run/hermes-shopping-canary.yml",
    "/root/hermes_restore/run/hermes-inventory-canary.yml",
    "/root/hermes_restore/run/hermes/hermes-family-canary.yml",
]

# DB mount
DB_MOUNT_SOURCE = "/var/lib/hermes/production-db/healbite.db"
DB_MOUNT_DESTINATION = "/home/hermes/healbite.db"

# Qdrant
QDRANT_COLLECTION = "healbite_memory_os"
