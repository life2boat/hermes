import os
from pathlib import Path
from typing import Any

from ai_engineering.production_runtime_attestation import (
    CollectorResult,
    CollectorStatus,
    create_collector_result,
)


class SecretSourceStructuralCollector:
    """Collects structural Secret Source state safely without reading contents."""

    collector_id = "secret_source_structural"

    def __init__(self, expected_path: str, legacy_path: str | None = None) -> None:
        self.expected_path = expected_path
        self.legacy_path = legacy_path

    def collect(self) -> CollectorResult:
        try:
            expected_p = Path(self.expected_path).resolve()
            
            observations: dict[str, Any] = {
                "approved_source_exists": False,
                "legacy_source_present": False,
            }

            if expected_p.exists():
                observations["approved_source_exists"] = True
                try:
                    stat = expected_p.stat()
                    # Check safe structural properties like mode and uid
                    # Just recording the octal mode is safe
                    observations["approved_source_mode"] = oct(stat.st_mode)[-4:]
                    observations["approved_source_uid"] = stat.st_uid
                    observations["approved_source_gid"] = stat.st_gid
                except OSError:
                    observations["approved_source_mode"] = "unknown"

            if self.legacy_path:
                legacy_p = Path(self.legacy_path).resolve()
                if legacy_p.exists():
                    observations["legacy_source_present"] = True

            return create_collector_result(
                self.collector_id, CollectorStatus.AVAILABLE, observations
            )

        except Exception:
            return create_collector_result(
                self.collector_id, CollectorStatus.UNAVAILABLE, {}
            )
