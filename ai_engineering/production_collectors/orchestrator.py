from datetime import timezone, datetime
UTC = timezone.utc
from typing import Sequence

from ai_engineering.production_runtime_attestation import (
    ProductionRuntimeAttestation,
    ProductionRuntimeCollector,
    create_attestation,
)


def collect_production_attestation(
    target: str,
    collectors: Sequence[ProductionRuntimeCollector],
) -> ProductionRuntimeAttestation:
    """Run read-only collectors and build a production runtime attestation.

    This function safely invokes each collector and aggregates their results.
    The `create_attestation` function internally calls `sanitize_evidence`
    on all observations to ensure no secret values are included in the final artifact.
    """
    collected_at_utc = datetime.now(UTC)
    results = []
    
    for collector in collectors:
        try:
            # We trust collectors to return a CollectorResult.
            # create_attestation -> sanitize_evidence ensures fail-closed sanitization.
            results.append(collector.collect())
        except Exception:
            # If a collector completely crashes, we shouldn't fail the whole process,
            # or maybe we should? The Collector itself is supposed to catch exceptions
            # and return CollectorStatus.UNAVAILABLE. If it didn't, let it bubble up,
            # because that means a bug in the collector implementation.
            raise
            
    return create_attestation(
        target=target,
        collected_at_utc=collected_at_utc,
        collectors=results,
    )
