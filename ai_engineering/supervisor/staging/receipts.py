from dataclasses import dataclass
from typing import Optional
from datetime import datetime

@dataclass
class StagingPreflightReceipt:
    success: bool
    timestamp: datetime
    error_message: Optional[str] = None
    target_id: Optional[str] = None

@dataclass
class StagingDeploymentReceipt:
    success: bool
    timestamp: datetime
    container_id: Optional[str] = None
    repo_digest: Optional[str] = None
    oci_revision: Optional[str] = None
    error_message: Optional[str] = None

@dataclass
class StagingHealthReceipt:
    success: bool
    timestamp: datetime
    is_healthy: bool
    error_message: Optional[str] = None

@dataclass
class StagingCanaryReceipt:
    success: bool
    timestamp: datetime
    canary_result: Optional[str] = None
    error_message: Optional[str] = None

@dataclass
class StagingRollbackReceipt:
    success: bool
    timestamp: datetime
    error_message: Optional[str] = None

@dataclass(frozen=True, slots=True)
class ExactImageAttestation:
    source_sha: str
    registry_digest: str
    config_digest: str
    oci_revision: str
    platform: str
