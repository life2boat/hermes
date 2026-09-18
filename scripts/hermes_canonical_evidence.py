SECRET_EVIDENCE_TYPE = 'production_secret_presence'
SECRET_EVIDENCE_FIELDS = [
    'schema_version', 'evidence_type', 'target_sha', 'status',
    'source_class', 'collected_at_utc', 'required_secrets',
    'evidence_digest', 'execution_provenance'
]

DB_EVIDENCE_TYPE = 'production_db_path_safety'
DB_EVIDENCE_FIELDS = [
    'schema_version', 'evidence_type', 'target_sha', 'status',
    'validator_id', 'validator_version', 'path_classification',
    'collected_at_utc', 'evidence_digest', 'execution_provenance'
]

SCHEMA_EVIDENCE_TYPE = 'production_schema_compatibility'
SCHEMA_EVIDENCE_FIELDS = [
    'schema_version', 'evidence_type', 'target_sha', 'status',
    'observed_schema', 'digest', 'user_version', 'actual_user_version',
    'expected_user_version', 'actual_schema_digest', 'expected_schema_digest',
    'schema_delta', 'migration_required', 'integrity_status',
    'foreign_key_violation_count', 'collected_at_utc',
    'evidence_digest', 'execution_provenance'
]

ROLLBACK_EVIDENCE_TYPE = 'rollback_ready'
ROLLBACK_EVIDENCE_FIELDS = [
    'schema_version', 'evidence_type', 'target_sha', 'status',
    'current_production_image_digest', 'current_production_oci_revision',
    'rollback_image_digest', 'rollback_image_resolvable', 'rollback_revision',
    'rollback_mechanism_id', 'same_compose_chain', 'database_restore_required',
    'schema_downgrade_required', 'rollback_health_required',
    'rollback_attempt_count_max', 'rollback_procedure_proven',
    'canonical_rehearsal_evidence', 'collected_at_utc',
    'evidence_digest', 'execution_provenance'
]

CREDENTIAL_RISK_EVIDENCE_TYPE = 'credential_risk'
CREDENTIAL_RISK_EVIDENCE_FIELDS = [
    'schema_version', 'evidence_type', 'target_sha', 'status',
    'credential_risk_status', 'collected_at_utc',
    'evidence_digest', 'execution_provenance'
]

TYPES_MAP = {
    'secret_evidence': SECRET_EVIDENCE_TYPE,
    'db_evidence': DB_EVIDENCE_TYPE,
    'schema_evidence': SCHEMA_EVIDENCE_TYPE,
    'rollback_evidence': ROLLBACK_EVIDENCE_TYPE,
    'credential_risk_evidence': CREDENTIAL_RISK_EVIDENCE_TYPE
}

EXPECTED_FIELDS_MAP = {
    'secret_evidence': SECRET_EVIDENCE_FIELDS,
    'db_evidence': DB_EVIDENCE_FIELDS,
    'schema_evidence': SCHEMA_EVIDENCE_FIELDS,
    'rollback_evidence': ROLLBACK_EVIDENCE_FIELDS,
    'credential_risk_evidence': CREDENTIAL_RISK_EVIDENCE_FIELDS
}
