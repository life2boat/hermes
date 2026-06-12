# RUNBOOK_MEMORY_OS

## Rebuild Qdrant Index

Run from the project root:

```bash
cd /home/hermes/.hermes/hermes-agent
MEMORY_VECTOR_ENABLED=true QDRANT_URL=http://127.0.0.1:6333 QDRANT_COLLECTION=healbite_memory_os venv/bin/python scripts/rebuild_qdrant_memory_index.py --db-path /home/hermes/healbite.db
```

Dry-run without touching Qdrant:

```bash
cd /home/hermes/.hermes/hermes-agent
MEMORY_VECTOR_ENABLED=false venv/bin/python scripts/rebuild_qdrant_memory_index.py --db-path /home/hermes/healbite.db --dry-run
```

## Check Qdrant Count

```bash
curl -sS -X POST http://127.0.0.1:6333/collections/healbite_memory_os/points/count   -H 'Content-Type: application/json'   -d '{}'
```

Check SQLite source-of-truth count:

```bash
sqlite3 /home/hermes/healbite.db 'SELECT COUNT(*) FROM memory_os_facts;'
```

## Enable Vector Search

Edit `/home/hermes/.hermes/.env`:

```bash
MEMORY_VECTOR_ENABLED=true
```

Then restart the bot:

```bash
docker restart hermes-bot
```

## Panic Button: Roll Back To SQLite-only

Edit `/home/hermes/.hermes/.env`:

```bash
MEMORY_VECTOR_ENABLED=false
```

Restart the bot:

```bash
docker restart hermes-bot
```

Qdrant may stay running; SQLite-only mode is controlled by the feature flag.
