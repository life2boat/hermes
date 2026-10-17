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
  - `signature_algorithm`: `"ed25519"`.
  - `signature`: 128-character hex signature over domain-separated `attestation_id`.
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
2. **Authenticity Verification**:
   - Verifies the digital signature using `cryptography.hazmat.primitives.asymmetric.ed25519`.
   - Signs the `attestation_id` with domain separation (`hermes-production-runtime-signature-v1\n`).
   - A digest alone without a valid trusted signature does not grant authenticity.
   - Private keys are **never** committed to Git, embedded in fixtures, or printed in reports.

---

## 4. Trust and Evidence Freshness Rules

An attestation is considered authentic and fresh only when:
- The signer's public key matches an approved key on the trusted public key ring (`--trusted-keys`).
- The evaluation timestamp is before `valid_until_utc`. If `now_utc > valid_until_utc`, verification yields `EXPIRED_EVIDENCE`.
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

## 6. Comparison Status Semantics

Comparison between `IntendedProductionState` and `ProductionRuntimeAttestation` returns strictly one of three statuses:

| Status | Meaning | Preconditions |
| :--- | :--- | :--- |
| `MATCH` | Complete alignment | Valid integrity, authentic signature, fresh timestamp, and all observed properties match expected state. |
| `DRIFT` | Proven divergence | Valid evidence proves a discrepancy in image digest, revision SHA, active services, or runtime contract. |
| `INSUFFICIENT_EVIDENCE` | Incomplete or unverified proof | Missing evidence, unavailable collectors, expired timestamp, or untrusted/invalid cryptographic signature. |

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
# Create an attestation from input JSON (optionally signing with Ed25519 private key)
python scripts/production_runtime_attestation.py create --input raw.json --output attestation.json [--private-key <hex_or_file>]

# Verify attestation integrity and authenticity against trusted public keys
python scripts/production_runtime_attestation.py verify --input attestation.json [--trusted-keys <hex_or_file>] [--output verified.json]

# Compare intended state against attestation
python scripts/production_runtime_attestation.py compare --intended intended.json --attestation attestation.json --output comparison.json [--trusted-keys <hex_or_file>]

# Inspect attestation details (human-readable or JSON)
python scripts/production_runtime_attestation.py inspect --input attestation.json [--json] [--output inspection.txt]

# Sanitize arbitrary input evidence JSON
python scripts/production_runtime_attestation.py sanitize --input raw.json --output safe.json
```

### Exit Codes
- `0`: Success / Comparison `MATCH`.
- `2`: Comparison `DRIFT` or `INSUFFICIENT_EVIDENCE`.
- `1`: Contract error, tampering detected, or invalid argument.

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
