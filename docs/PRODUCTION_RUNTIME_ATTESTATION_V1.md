# ProductionRuntimeAttestation v1

ProductionRuntimeAttestation v1 is an offline, deterministic evidence contract.
It records already-collected, sanitized observations about an identified target;
it does not grant runtime authority and does not contain a live collector.

## Contract

An attestation contains a schema version, target, UTC collection timestamp,
ordered collector results, and a SHA-256 `attestation_id` over the canonical
content excluding the ID itself. Canonical JSON is UTF-8, uses sorted object
keys and compact separators, and rejects duplicate keys and non-finite numbers.
Deserialization recomputes the ID and fails closed on structural or content
tampering.

`ProductionRuntimeCollector` is a protocol only. Implementations that reach
Docker, databases, Qdrant, Telegram, WSL, networks, or production belong to a
separately authorized later sprint. B1 imports and invokes none of them.

## Evidence safety

Callers submit observations through the constructors or the `sanitize`
command. Credential-bearing keys and recognizable inline credentials become
the literal `<REDACTED>` marker before persistence. Verification rejects
recognizable unsanitized secret material. The contract never stores derived
secret hashes, fingerprints, lengths, prefixes, or suffixes. Non-secret
structural booleans such as whether an approved source exists are permitted.

## Intended state and comparison

`IntendedProductionState` declares required collector observations for one
target. Comparison is deterministic:

- any proven mismatch produces `DRIFT`, even when unrelated evidence is absent;
- absent required evidence without proven drift produces
  `INSUFFICIENT_EVIDENCE`;
- complete matching evidence produces `MATCH`;
- different targets are a contract error.

Comparison output identifies observation paths only and never copies observed
or intended values.

## Offline CLI

```text
python scripts/production_runtime_attestation.py create --input raw.json --output attestation.json
python scripts/production_runtime_attestation.py verify --input attestation.json --output verified.json
python scripts/production_runtime_attestation.py compare --intended intended.json --attestation attestation.json --output comparison.json
python scripts/production_runtime_attestation.py sanitize --input raw.json --output sanitized.json
```

`compare` returns 0 for `MATCH`, 2 for `DRIFT` or
`INSUFFICIENT_EVIDENCE`, and 1 for malformed, tampered, or otherwise invalid
contracts. Output paths are create-only to avoid silent evidence replacement.

## B1: Offline Contracts

The core B1 contracts and CLI operate offline. They do not invoke Docker, databases, Qdrant, Telegram, WSL, networks, or production systems.

## B2: Read-Only Production Collectors

Sprint B2 implements read-only production collection for WSL2 Ubuntu:
- **Docker**: read-only container state inspection.
- **SQLite**: explicitly restricted to `mode=ro` URI connections.
- **Qdrant**: hardcoded localhost-only network boundary and read operations only.
- **Secret Source Structural**: structural presence and file mode checks with strict value isolation.

Live collection enforces the following safety invariants:
- **Exact-source guard**: execution requires an explicit `--expected-source-sha` matching a verified `HEAD`.
- **Clean-tree guard**: execution is blocked if the repository worktree is dirty.
- **Create-only evidence**: output paths refuse to overwrite existing artifacts.
- **Post-health check**: a structural health verification runs after evidence generation.
