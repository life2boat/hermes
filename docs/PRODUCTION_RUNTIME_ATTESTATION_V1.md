# ProductionRuntimeAttestation v1

> **Important Boundary:** `Sprint B1 does not collect live production evidence.`

`ProductionRuntimeAttestation v1` provides an offline, deterministic evidence and verification contract designed to answer the core question:

**Does the actual observed state of running Hermes correspond to what should be deployed according to an approved release?**

Sprint B1 implements the schema, canonical serialization, cryptographic signatures, secret sanitization, comparator logic, offline collector interfaces, and offline CLI. No production network calls, live databases, live Docker daemons, or SSH sessions are used.

---

## 1. Schema Specification

The attestation format is formally specified in [`schemas/production-runtime-attestation-v1.schema.json`](../schemas/production-runtime-attestation-v1.schema.json). It supports two shapes:

### Canonical Full v1 Shape
- `schema_version`: Must be integer `1`.
- `attestation_id`: 64-character hex string representing the SHA-256 hash over canonical content with domain separation.
- `canonical_repository`: Normalized Git repository URI (e.g. `https://github.com/life2boat/hermes.git`).
- `canonical_main_sha`: 40-character hex commit SHA from canonical `main`.
- `environment_id`: Target runtime environment identifier (e.g. `production`, `staging`).
- `collection_context_id`: Safe identifier distinguishing the collection run.
- `observed_at`: Strict ISO 8601 UTC timestamp (`YYYY-MM-DDTHH:MM:SSZ`).
- `collector_id`: Safe identifier for the reporting collector mechanism.
- `collector_version`: Semantic version string of the collector.
- `evidence_provenance`: Object or `null` containing cryptographic proof:
  - `signer_identity`: 64-character hex representation of the Ed25519 public key.
  - `signature_algorithm`: `"ed25519-v2"`.
  - `signature`: 128-character hex signature over canonical bound metadata payload.
  - `signature_contract_version`: Integer contract version (`2`).
  - `signed_at_utc`: Strict ISO 8601 UTC timestamp.
  - `valid_until_utc`: Strict ISO 8601 UTC expiration timestamp.
- `observed_runtime_state`: Object containing verifiable, non-secret runtime properties:
  - `image_digest`: OCI image digest (e.g. `sha256:...`).
  - `oci_revision`: Git commit or OCI revision SHA.
  - `release_sha`: Verified repository release SHA.
  - `runtime_contract`: Contract version string (e.g. `hermes-runtime-v1`).
  - `schema_version`: Database/system schema version.
  - `active_services`: Sorted sequence of active service names.
  - `configuration_contract_version`: Contract configuration version string.
  - `release_identity`: Verified release identity string (e.g. `hermes-v2-prod`).
  - `raw_observations`: Optional key-value dictionary of non-secret observations.
- `verification_metadata`: Object or `null` tracking offline verification receipts.

### Compact / Backward-Compatible Shape
- `schema_version`: Integer `1`.
- `attestation_id`: 64-character hex string.
- `target`: Target identifier string.
- `collected_at_utc`: Strict ISO 8601 UTC timestamp.
- `collectors`: Sequence of `{collector_id, status, observations}` objects.
- Optional `evidence_provenance` and `verification_metadata`.

---

## 2. Canonical Serialization Contract

To guarantee cross-platform determinism across Windows, Linux, and CI:
- **Encoding**: Strict UTF-8 without byte-order marks (`BOM`).
- **Separators**: Compact separators without whitespace: `(`,`, `:`)`.
- **Key Ordering**: All JSON object keys are recursively sorted lexicographically (`sort_keys=True`).
- **Floating Point Numbers**: Non-finite numbers (`NaN`, `Infinity`, `-Infinity`) are strictly rejected.
- **Line Endings**: Payloads do not contain unescaped newline characters.
- **Round-Trip Invariance**: `serialize(deserialize(payload)) == payload`.

---

## 3. Integrity and Authenticity Model

Verification operates on two independent, non-interchangeable tiers:

```text
                  +-----------------------------+
                  |  Attestation Payload (JSON) |
                  +-----------------------------+
                                 |
                 +---------------+---------------+
                 |                               |
                 v                               v
    +------------------------+      +--------------------------+
    |   Tier 1: Integrity   |      |   Tier 2: Authenticity   |
    | Canonical SHA-256 Hash |      | Ed25519 Digital Signature|
    | (Detects tampering)    |      | (Proves trusted origin)  |
    +------------------------+      +--------------------------+
```

1. **Integrity Verification**:
   - Recomputes the SHA-256 digest of the canonical bytes with domain separation prefix (`hermes-production-runtime-attestation-v1\n`).
   - If the recorded `attestation_id` does not match the recomputed digest, the operation fails closed with `TAMPERED_ATTESTATION_ID`.
2. **Authenticity Verification (v1.1 Security Remediation)**:
   - Verifies the digital signature using `cryptography.hazmat.primitives.asymmetric.ed25519` under contract version 2 (`ed25519-v2`).
   - Cryptographically binds validity metadata in canonical JSON under prefix `hermes-production-runtime-signature-v2\n`:
     - `attestation_id`
     - `canonical_repository`
     - `environment_id`
     - `collection_context_id`
     - `signer_identity`
     - `signed_at_utc`
     - `valid_until_utc`
     - `signature_contract_version`
   - Any tampering with timestamps, attestation ID, or environment identifiers breaks the cryptographic signature.
   - An unsigned digest alone or evidence without a trusted key store strictly yields `INSUFFICIENT_EVIDENCE` (no unsigned `MATCH`).
   - Private keys are **never** committed to Git, embedded in fixtures, passed as raw CLI arguments, or printed in reports.

---

## 4. Trust and Evidence Freshness Rules

An attestation is considered authentic and fresh only when:
- The signer's public key matches an approved key on the trusted public key ring (`--trusted-keys`).
- Clock skew tolerance (`MAX_CLOCK_SKEW_SECONDS = 300`): timestamps cannot be skewed into the future beyond 300s.
- Sequence ordering: `signed_at_utc >= observed_at - clock_skew` and `valid_until_utc > signed_at_utc`.
- Expiration: `current_time_utc <= valid_until_utc`. If expired, verification yields `EXPIRED_EVIDENCE`.
- Max evidence age (`DEFAULT_MAX_EVIDENCE_AGE_SECONDS = 86400`): evidence older than 24 hours yields `STALE_EVIDENCE`.
- By default, freshness evaluates against the verifiable current UTC time (`datetime.now(UTC)`). Time injection is supported for deterministic tests and offline CLI audits.
- The target environment matches the intended target (`TARGET_MISMATCH` is a contract error).

---

## 5. IntendedProductionState Model

`IntendedProductionState` specifies the expected verifiable invariants derived from approved release artifacts:
- `canonical_repository`: Approved source repository.
- `intended_main_sha`: Approved commit SHA.
- `intended_image_digest`: Approved container image digest.
- `intended_oci_revision`: Expected revision tag.
- `expected_service_set`: Expected active services (e.g. `("gateway",)`).
- `expected_runtime_contract`: Target runtime contract identifier.
- `expected_schema_version`: Target schema version.
- `expected_configuration_contract_version`: Target configuration version.
- `expected_release_identity`: Target release identifier.

Credential values, private keys, environment secrets, and secret lengths are strictly excluded.

---

## 6. Comparison Status Semantics & Evaluation Modes

Comparison between `IntendedProductionState` and `ProductionRuntimeAttestation` returns strictly one of three statuses:

| Status | Meaning | Preconditions |
| :--- | :--- | :--- |
| `MATCH` | Complete alignment | Valid integrity, authentic signature, fresh timestamp, and all observed properties match expected state. |
| `DRIFT` | Proven divergence | Valid evidence proves a discrepancy in image digest, revision SHA, active services, release identity, or runtime contract. |
| `INSUFFICIENT_EVIDENCE` | Incomplete or unverified proof | Missing evidence, unavailable collectors, missing mandatory invariants, expired timestamp, or untrusted/invalid signature. |

### Evaluation Modes and Authority
- **Authoritative Mode (`AUTHORITATIVE_PRODUCTION`)**:
  - Activated when `current_time_utc is None` (evaluating against current system UTC).
  - Yields `authoritative_for_production = True` only upon authentic `MATCH` with non-dummy observed runtime state.
  - Required for production release-gating.
- **Diagnostic Mode (`DIAGNOSTIC_TIME_OVERRIDE`)**:
  - Activated whenever a manual reference time (`current_time_utc`) is supplied for testing or forensic replay.
  - Strictly enforces `authoritative_for_production = False` regardless of whether invariants match.
  - Release-gating tools reject non-authoritative comparisons (CLI exits with code `3`).

### Strict Reason Codes and Co-Occurrence Invariants
All comparison results record stable, programmatic reason codes:
- Missing invariant: `MISSING_OBSERVED_INVARIANT:<field>`
- Invariant drift: `INVARIANT_DRIFT:<field>`
- Service mismatch: `MISSING_SERVICE:<name>`, `EXTRA_SERVICE_DETECTED`, `EXTRA_SERVICE:<name>`
- Policy violations: `UNTRUSTED_FRESHNESS_POLICY`, `EMPTY_RUNTIME_OBSERVATIONS`, `LEGACY_CONTRACT_NOT_AUTHORITATIVE`
- Signature verification: `MISSING_TRUST_STORE`, `MISSING_EVIDENCE_PROVENANCE`, `UNTRUSTED_SIGNATURE_KEY`, `EXPIRED_EVIDENCE`, `STALE_EVIDENCE`

> **Fail-Closed Co-Occurrence Rule**: If missing invariants and drift co-occur in the same evaluation, the comparison status **strictly resolves to `INSUFFICIENT_EVIDENCE`**. Proven divergence cannot override incomplete proof.

### Cryptographic Comparison ID Binding (v1.2)
All semantically significant comparison fields (`attestation_id`, `authoritative_for_production`, `drifted_observations`, `evaluation_mode`, `intended_digest`, `missing_observations`, `reason_codes`, `schema_version`, `status`, `target`) are canonicalized and bound into `comparison_id` under domain prefix `hermes-production-runtime-comparison-v2\n`. Tampering with authority flags, evaluation modes, or reason codes fails closed with `TAMPERED_COMPARISON_ID`.

---

## 7. Collector Interface Contract

Collector interfaces define the structural contract for future collection without live access:

```python
class RuntimeCollector(Protocol):
    collector_id: str
    collector_version: str

    def collect_identity(self) -> dict[str, str]: ...
    def collect_image_metadata(self) -> dict[str, str]: ...
    def collect_service_state(self) -> dict[str, Any]: ...
    def collect_runtime_contract(self) -> dict[str, str]: ...
    def collect_schema_metadata(self) -> dict[str, Any]: ...
```

- `MockRuntimeCollector`: Deterministic in-memory collector for test execution.
- `StaticFixtureCollector`: In-memory collector backed by static dictionary fixtures.
- Live collectors (Docker, SQLite, Qdrant) are strictly prohibited from execution in Sprint B1.

---

## 8. Secret Sanitization Policy

- **Allowlist-Only Serialization**: Only explicitly declared schema properties are persisted.
- **Sensitive Key Masking**: Any key matching sensitive patterns (`*token*`, `*key*`, `*secret*`, `*password*`, `*credential*`) is replaced with `<REDACTED>`.
- **Inline Credential Redaction**: Regular expressions and `scripts/secret_scanner.py` scan string values and redact tokens (`Bearer ...`, `ghp_...`, `sk-...`).
- **Zero Information Leak**: Secret lengths, partial prefixes, and credential hashes are forbidden in output JSON, comparison reports, and exception messages.
- **Fail-Closed Deserialization**: Incoming documents containing unmasked secret patterns are rejected immediately.

---

## 9. Offline CLI Usage

The CLI is located at `scripts/production_runtime_attestation.py` and runs entirely offline:

```powershell
# Create an attestation from input JSON (optionally signing with Ed25519 private key from file)
python scripts/production_runtime_attestation.py create --input raw.json --output attestation.json [--private-key-file <path_to_key_file>] [--signed-at-utc <iso_utc>]

# Verify attestation authenticity against trusted public keys (fails closed if keys/provenance are missing)
python scripts/production_runtime_attestation.py verify --input attestation.json --trusted-keys <hex_or_file> [--output verified.json]

# Verify structural integrity only without cryptographic authenticity verification
python scripts/production_runtime_attestation.py verify --input attestation.json --integrity-only [--output verified.json]

# Compare intended state against attestation (fails closed with exit code 2 if unsigned or untrusted; exit code 3 on diagnostic match)
python scripts/production_runtime_attestation.py compare --intended intended.json --attestation attestation.json --output comparison.json --trusted-keys <hex_or_file> [--current-time-utc <iso_utc>]

# Inspect attestation details (human-readable or JSON)
python scripts/production_runtime_attestation.py inspect --input attestation.json [--json] [--output inspection.txt]

# Sanitize arbitrary input evidence JSON
python scripts/production_runtime_attestation.py sanitize --input raw.json --output safe.json
```

### Exit Codes
- `0`: Authoritative `MATCH` or Authoritative `verify` (authentic, fresh evidence verified with system UTC without time override).
- `3`: Diagnostic `MATCH` or Diagnostic `verify` with manual time override (non-authoritative result; fails release-gating to prevent unearned production authority).
- `2`: Comparison `DRIFT` or `INSUFFICIENT_EVIDENCE`.
- `1`: Contract error, tampering detected, missing trust store, or invalid argument.

---

## 10. Synthetic JSON Artifact Examples

### Synthetic Attestation (`attestation.json`)
```json
{
  "attestation_id": "fbc1f31230ad6596ac89ee16ab1b01e76b9bf2ff1f27579970bcbf9209ebb73f",
  "collected_at_utc": "2026-01-02T03:04:05Z",
  "collectors": [
    {
      "collector_id": "runtime",
      "observations": {
        "image_revision": "abc123",
        "running": true
      },
      "status": "AVAILABLE"
    },
    {
      "collector_id": "storage",
      "observations": {
        "integrity": "ok"
      },
      "status": "AVAILABLE"
    }
  ],
  "schema_version": 1,
  "target": "synthetic-prod"
}
```

### Intended State (`intended.json`)
```json
{
  "expected_collectors": [
    {
      "collector_id": "runtime",
      "observations": {
        "image_revision": "abc123",
        "running": true
      }
    },
    {
      "collector_id": "storage",
      "observations": {
        "integrity": "ok"
      }
    }
  ],
  "schema_version": 1,
  "target": "synthetic-prod"
}
```

### Comparison Result (`comparison.json`)
```json
{
  "attestation_id": "fbc1f31230ad6596ac89ee16ab1b01e76b9bf2ff1f27579970bcbf9209ebb73f",
  "comparison_id": "90ba95e4e7e6f3df80f7bc603c7e39a3f2d26d30b91d293fbfb53c613e51a613",
  "drifted_observations": [],
  "intended_digest": "4eeea0c868772bf2df5d5c0663db6e974e6449ddfae407db3da7c74233e7eeb5",
  "missing_observations": [],
  "schema_version": 1,
  "status": "MATCH",
  "target": "synthetic-prod"
}
```

---

## 11. Future Live-Collector Boundary

Sprint B2 introduces read-only live collectors under strict guards:
- **Read-Only SQLite**: Opened exclusively with URI `file:...mode=ro`.
- **Localhost-Only Qdrant**: Restricted to `localhost:6333` with read-only calls.
- **Docker Inspection**: Restricted to `docker inspect` without modification authority.
- **Exact-Source Guard**: Collectors check `--expected-source-sha` against repository `HEAD`.
- **Clean Worktree Guard**: Refuses execution if uncommitted changes exist.
- **Create-Only Output**: Artifacts never overwrite existing files silently.

Sprint B1 maintains total isolation from live runtime environments.
