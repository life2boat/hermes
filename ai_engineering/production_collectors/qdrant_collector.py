import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ai_engineering.production_runtime_attestation import (
    CollectorResult,
    CollectorStatus,
    create_collector_result,
)

MAX_QDRANT_RESPONSE_BYTES = 16384  # 16KB strict limit


class QdrantReadOnlyCollector:
    """Collects structural Qdrant state safely without credentials."""

    collector_id = "qdrant_read_only"

    def __init__(self, endpoint_url: str, collection_name: str) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        self.collection_name = collection_name

    def collect(self) -> CollectorResult:
        try:
            # 0. Check safe local target policy
            parsed_url = urllib.parse.urlparse(self.endpoint_url)
            if parsed_url.hostname not in ("localhost", "127.0.0.1", "::1"):
                return create_collector_result(
                    self.collector_id,
                    CollectorStatus.UNAVAILABLE,
                    {},
                )

            # 1. Check general Qdrant health without credentials
            health_url = f"{self.endpoint_url}/healthz"
            req = urllib.request.Request(health_url, method="GET")

            reachable = True
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    if response.status != 200:
                        reachable = False
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    # We are not allowed to use credentials to fetch evidence
                    return create_collector_result(
                        self.collector_id,
                        CollectorStatus.UNAVAILABLE,
                        {},
                    )
                reachable = False
            except urllib.error.URLError:
                reachable = False

            if not reachable:
                return create_collector_result(
                    self.collector_id,
                    CollectorStatus.UNAVAILABLE,
                    {},
                )

            # 2. Check collection presence (may require credentials if auth is enforced globally)
            collection_url = f"{self.endpoint_url}/collections/{self.collection_name}"
            req_col = urllib.request.Request(collection_url, method="GET")

            observations: dict[str, Any] = {
                "reachable": True,
                "collection_exists": False,
                "collection_status": "unknown",
            }

            try:
                with urllib.request.urlopen(req_col, timeout=5) as response:
                    if response.status == 200:
                        raw_data = response.read(MAX_QDRANT_RESPONSE_BYTES + 1)
                        if len(raw_data) > MAX_QDRANT_RESPONSE_BYTES:
                            return create_collector_result(
                                self.collector_id,
                                CollectorStatus.UNAVAILABLE,
                                {},
                            )
                        data = json.loads(raw_data.decode("utf-8"))
                        if data.get("result", {}).get("status"):
                            observations["collection_exists"] = True
                            observations["collection_status"] = data["result"]["status"]
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    observations["collection_exists"] = False
                elif e.code in (401, 403):
                    return create_collector_result(
                        self.collector_id,
                        CollectorStatus.UNAVAILABLE,
                        {},
                    )
                else:
                    observations["collection_status"] = f"error_http_{e.code}"
            except Exception:
                observations["collection_status"] = "error_fetching"

            return create_collector_result(
                self.collector_id, CollectorStatus.AVAILABLE, observations
            )

        except Exception:
            return create_collector_result(
                self.collector_id, CollectorStatus.UNAVAILABLE, {}
            )
