# ProductionRuntimeAttestation v1

> **Important Boundary:** `Sprint B1 does not collect live production evidence.`

`ProductionRuntimeAttestation v1` provides an offline, deterministic evidence and verification contract designed to answer the core question:

**Does the actual observed state of running Hermes correspond to what should be deployed according to an approved release?**

Sprint B1 implements the schema, canonical serialization, cryptographic signatures, secret sanitization, comparator logic, offline collector interfaces, and offline CLI. No production network calls, live databases, live Docker daemons, or SSH sessions are used.

---

## 1. Schema Specifications

The contracts are formally specified across dedicated JSON schemas:
- **Attestation Schema**: [`schemas/production-runtime-attestation-v1.schema.json`](../schemas/production-runtime-attestation-v1.schema.json)
- **Comparison v3 Schema**: [`schemas/production-runtime-comparison-v1.schema.json`](../schemas/production-runtime-comparison-v1.schema.json) (alias [`schemas/production-runtime-comparison-v3.schema.json`](../schemas/production-runtime-comparison-v3.schema.json))
- **Intended State Schema**: [`schemas/production-runtime-intended-v1.schema.json`](../schemas/production-runtime-intended-v1.schema.json)

### Attestation Schema Shapes

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
- `canonical_repository`: Approved source repository URI (non-empty).
- `intended_main_sha`: Approved 40-character commit SHA (non-empty, non-zero).
- `intended_image_digest`: Approved container image digest (`sha256:...`, non-empty, non-zero).
- `intended_oci_revision`: Expected 40-character commit or OCI revision tag (non-empty, non-zero).
- `expected_service_set`: Expected active services sequence (non-empty tuple, e.g. `("gateway",)`).
- `expected_runtime_contract`: Target runtime contract identifier (non-empty).
- `expected_schema_version`: Target schema version (integer > 0).
- `expected_configuration_contract_version`: Target configuration version (non-empty).
- `expected_release_identity`: Target release identifier (non-empty).

All 9 fields are mandatory for production authorization. No fallback placeholder defaults (e.g. `"0"*40` or dummy digests) are substituted. If any required invariant is missing, empty, or a zero-placeholder, `compare_production_runtime()` fails closed with:
- `status = ComparisonStatus.INSUFFICIENT_EVIDENCE`
- `authoritative_for_production = False`
- `reason_codes` containing `MISSING_INTENDED_INVARIANT:<field>`
- `missing_observations` containing `intended_invariant:<field>`

### Strict Intended State Validation (v1.2.3)
Both direct dataclass instantiation via `validate_intended_state()` and JSON deserialization via `deserialize_intended_state()` strictly enforce structural integrity:
- `schema_version`: Must be integer `1` (booleans strictly rejected).
- `target`: Must be a valid identifier (`^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$`).
- `intended_main_sha`: If non-empty, must match 40-character hex pattern (`^[0-9a-f]{40}$`), or raise `SHA_INVALID`.
- `intended_image_digest`: If non-empty, must match 64-hex digest or `sha256:<64-hex>`, or raise `DIGEST_INVALID`.
- `intended_oci_revision`: If non-empty, must match 40-character hex pattern, or raise `REVISION_INVALID`.
- `expected_schema_version`: Must be non-negative integer (booleans and negative values raise `SCHEMA_VERSION_INVALID`).
- `expected_service_set`: Must be a sequence of valid identifiers (invalid types raise `SERVICE_SET_INVALID`).
- `expected_collectors`: Duplicate collector IDs raise `DUPLICATE_COLLECTOR`, unsorted IDs raise `COLLECTOR_ORDER_INVALID`.

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

### Cryptographic Comparison ID Binding & Consistency Rules (v1.2.2)
All semantically significant comparison fields (`attestation_id`, `authoritative_for_production`, `drifted_observations`, `evaluated_at_utc`, `evaluation_mode`, `intended_digest`, `missing_observations`, `reason_codes`, `schema_version`, `status`, `target`) are canonicalized and bound into `comparison_id` under domain prefix `hermes-production-runtime-comparison-v3\n` (`DOMAIN_SEPARATOR_COMPARISON_V3`).
- **Timestamp Integrity**: `evaluated_at_utc` is bound into the comparison hash. Any tampering with timestamps raises `TAMPERED_COMPARISON_ID`.
- **Authority Consistency**: If `authoritative_for_production` is `True`, validation strictly requires:
  - `status == ComparisonStatus.MATCH` (otherwise `INCONSISTENT_STATUS_AUTHORITY`)
  - `evaluation_mode == "AUTHORITATIVE_PRODUCTION"` (otherwise `INCONSISTENT_EVALUATION_MODE`)
  - `drifted_observations` is empty and `missing_observations` is empty (otherwise `INCONSISTENT_STATUS_AUTHORITY`)
  - `reason_codes` contains zero diagnostic or legacy codes (otherwise `DIAGNOSTIC_REASON_CANNOT_AUTHORIZE_MATCH`)
- **Legacy Comparison Exclusion**: Legacy comparisons hashed with domain prefix v1 or v2 cannot authorize production. If evaluated or validated with `authoritative_for_production = True`, validation fails closed with `LEGACY_COMPARISON_CANNOT_BE_AUTHORITATIVE`.

### Formal JSON Schemas (v1.2.3 / v1.2.4)
Formal JSON Schema Draft 2020-12 definitions are maintained in `schemas/`:
- `schemas/production-runtime-intended-v1.schema.json`: Enforces strict types and formats for intended state, requiring `schema_version = 1`, 40-hex commit SHAs, canonical digest patterns (`^((sha256:)?[0-9a-f]{64})?$`), `uniqueItems: true` for collector arrays, and rejection of unknown properties.
- `schemas/production-runtime-attestation-v1.schema.json`: Formal schema for attestation payloads, enforcing 40-hex SHAs, canonical image digests, `uniqueItems: true` on collector sequences, and valid RFC 3339 timestamps.
- `schemas/production-runtime-comparison-v3.schema.json`: Formal schema for comparison results supporting canonical v3 and legacy v1 compact representations. For canonical v3 results, conditional validation (`allOf` / `if-then`) strictly enforces that when `authoritative_for_production: true`:
  - `status` MUST be `"MATCH"`
  - `evaluation_mode` MUST be `"AUTHORITATIVE_PRODUCTION"`
  - `drifted_observations` and `missing_observations` MUST be empty (`maxItems: 0`)
  - Evaluation mode must be one of `["AUTHORITATIVE_PRODUCTION", "DIAGNOSTIC_TIME_OVERRIDE", "LEGACY_UNSPECIFIED"]`
- `schemas/production-runtime-comparison-v1.schema.json`: Alias schema referencing comparison definitions aligned with schema version 1.

### Multi-Tiered Collector Uniqueness Model (v1.2.4)
Standard JSON Schema Draft 2020-12 specification lacks syntax to enforce uniqueness of an inner object property (`collector_id`) across elements of an array (its `uniqueItems` keyword only evaluates whole-item value equality). The repository establishes an explicit two-tiered validation architecture:
1. **Tier 1 (Syntactic Schema)**: Standard Draft 2020-12 schemas validate structural types, formats, required properties, and exact duplicate element rejection via `uniqueItems: true`.
2. **Tier 2 (Semantic Domain)**: Python validators (`validate_intended_state`, `validate_attestation`, `create_intended_state`, `create_attestation`) strictly enforce property-level uniqueness (`DUPLICATE_COLLECTOR`) and ascending alphanumeric sorting (`COLLECTOR_ORDER_INVALID`).

### Empty and Zero Digest Prohibition
Empty strings (`""`), all-zero digests (`"0" * 64`), and sha256-prefixed all-zero digests (`"sha256:" + "0" * 64`), as well as all-zero commit SHAs (`"0" * 40`), are unconditionally rejected from authorizing production. Even if both intended state and observed state specify matching empty or zero placeholders, the comparison independently records missing invariants, strictly resolves to `INSUFFICIENT_EVIDENCE`, and enforces `authoritative_for_production = False`.

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
- `0`: Authoritative `MATCH` or Authoritative `verify` (authentic, fresh evidence verified with system UTC without time override; outputs `verification_mode=AUTHORITATIVE_UTC`).
- `4`: `verify --integrity-only` (integrity verified, unauthenticated/non-authoritative proof; outputs `verification_mode=INTEGRITY_ONLY: INTEGRITY_ONLY_UNAUTHENTICATED`).
- `3`: Diagnostic `MATCH` or Diagnostic `verify` with manual time override (non-authoritative result; outputs `verification_mode=DIAGNOSTIC_TIME_OVERRIDE`; fails release-gating to prevent unearned production authority).
- `2`: Comparison `DRIFT` or `INSUFFICIENT_EVIDENCE`.
- `1`: Contract error, tampering detected, missing trust store, conflicting CLI options (e.g. `--integrity-only` with `--current-time-utc`), or invalid argument.

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
